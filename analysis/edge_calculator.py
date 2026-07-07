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
    drift: Optional[str] = None,
) -> Dict:
    """
    Historical probability that price stays above a threshold [strike_distance_pct] below
    current, over [expiration_days], based on the stock's realized volatility structure.

    C1 FIX — drift removed. Previously this replayed RAW prices, so the result was dominated by
    the sample period's directional drift: the same OTM put scored strong positive edge in a
    bull sample and negative edge in a flat/down sample. That measures trend, not the volatility
    risk premium the system claims to harvest. We now work in log-return space, subtract the
    realized mean drift, and add back a small risk-free drift (config.TRUE_POP_DRIFT_MODE):

        "risk_free" (default): demean, then add RISK_FREE_RATE/252  → near-risk-neutral, directly
                               comparable to the option's implied probability (1 − |delta|)
        "zero":                demean only (pure zero-drift dispersion)
        "raw":                 legacy behavior (keeps drift — for A/B comparison only)

    M3 FIX — confidence is now based on the number of INDEPENDENT (non-overlapping) windows
    (total / expiration_days), not the raw overlapping-window count which overstated significance.

    Args:
        strike_distance_pct: decimal (e.g., 0.05 for a threshold 5% below current price)
        expiration_days: holding horizon in calendar/trading days
        historical_prices: pd.Series of close prices (2+ years preferred)
        drift: override for config.TRUE_POP_DRIFT_MODE

    Returns:
        true_pop: float 0-1  (probability end value exceeds the threshold)
        windows_tested: int  (overlapping windows sampled)
        independent_windows: float
        confidence: "HIGH" | "MEDIUM" | "LOW"
        drift_mode: str
    """
    prices = historical_prices.dropna().values.astype(float)
    n = len(prices)

    if n < expiration_days + 30:
        logger.warning("[edge] Insufficient history for true POP calculation")
        return {
            "true_pop": None,
            "windows_tested": 0,
            "independent_windows": 0,
            "confidence": "LOW",
            "drift_mode": drift or getattr(config, "TRUE_POP_DRIFT_MODE", "risk_free"),
        }

    mode = drift or getattr(config, "TRUE_POP_DRIFT_MODE", "risk_free")

    # Work in log-return space so drift can be removed cleanly.
    with np.errstate(divide="ignore", invalid="ignore"):
        log_returns = np.diff(np.log(prices))
    log_returns = log_returns[np.isfinite(log_returns)]
    if log_returns.size < expiration_days:
        return {"true_pop": None, "windows_tested": 0, "independent_windows": 0,
                "confidence": "LOW", "drift_mode": mode}

    if mode == "raw":
        adj = log_returns
    else:
        adj = log_returns - log_returns.mean()          # remove realized drift
        if mode == "risk_free":
            adj = adj + (getattr(config, "RISK_FREE_RATE", 0.04) / 252.0)

    # Cumulative product over each rolling window == end/start growth ratio under the chosen drift.
    threshold = 1.0 - strike_distance_pct
    cumsum = np.concatenate(([0.0], np.cumsum(adj)))
    m = adj.size
    successes = 0
    total = 0
    for i in range(m - expiration_days + 1):
        growth = np.exp(cumsum[i + expiration_days] - cumsum[i])
        if growth > threshold:
            successes += 1
        total += 1

    if total == 0:
        return {"true_pop": None, "windows_tested": 0, "independent_windows": 0,
                "confidence": "LOW", "drift_mode": mode}

    true_pop = successes / total
    independent = total / max(1, expiration_days)   # M3: effective non-overlapping sample size

    if independent >= 12:
        confidence = "HIGH"
    elif independent >= 5:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return {
        "true_pop": round(true_pop, 4),
        "windows_tested": total,
        "independent_windows": round(independent, 1),
        "confidence": confidence,
        "drift_mode": mode,
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


def calculate_fundamentals_score(fundamentals: Dict, is_etf: bool = False) -> Dict:
    """
    Score fundamentals on a 0-10 stability scale for short-duration premium selling.

    This is intentionally a risk filter (balance sheet / profitability / growth trend),
    not a long-term intrinsic-value model.
    """
    if is_etf:
        return {
            "score": 8,
            "blocking": False,
            "reasons": ["ETF: diversified fundamentals baseline"],
        }

    reasons = []
    penalties = 0

    debt_to_equity = fundamentals.get("debt_to_equity")
    if debt_to_equity is not None and debt_to_equity > 250:
        penalties += 2
        reasons.append("Very high debt-to-equity")

    current_ratio = fundamentals.get("current_ratio")
    if current_ratio is not None and current_ratio < 1.0:
        penalties += 1
        reasons.append("Current ratio below 1.0")

    profit_margin = fundamentals.get("profit_margin")
    if profit_margin is not None and profit_margin < 0:
        penalties += 2
        reasons.append("Negative profit margin")

    operating_margin = fundamentals.get("operating_margin")
    if operating_margin is not None and operating_margin < 0:
        penalties += 1
        reasons.append("Negative operating margin")

    revenue_growth = fundamentals.get("revenue_growth")
    if revenue_growth is not None and revenue_growth < -0.10:
        penalties += 1
        reasons.append("Revenue contracting >10%")

    earnings_growth = fundamentals.get("earnings_growth")
    if earnings_growth is not None and earnings_growth < -0.20:
        penalties += 2
        reasons.append("Earnings contracting >20%")

    free_cashflow = fundamentals.get("free_cashflow")
    if free_cashflow is not None and free_cashflow < 0:
        penalties += 1
        reasons.append("Negative free cash flow")

    recommendation = (fundamentals.get("analyst_recommendation") or "").lower()
    if recommendation in {"sell", "strong_sell"}:
        penalties += 1
        reasons.append(f"Analyst recommendation: {recommendation}")

    score = max(0, 10 - penalties)
    blocking = score <= 2
    if not reasons:
        reasons.append("No major fundamentals stress flags")

    return {
        "score": score,
        "blocking": blocking,
        "reasons": reasons,
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
    fundamentals_score: Optional[float] = None,
) -> Dict:
    """
    Composite 0-100 edge score combining all factors.

    Components:
      VRP component         (30 points max)
      True POP Edge         (25 points max)
    Technical Score       (20 points max)
      News Sentiment        (10 points max)
    Earnings Safety       (5 points max)
    Fundamentals          (10 points max)

    Returns:
        total_score: int 0-100
        component_breakdown: dict of each component's score
        qualified: True if total >= MIN_EDGE_SCORE
        disqualification_reason: str if disqualified early
    """
    breakdown = {}
    disqualification_reason = None

    # ── VRP Component (30 points max) ──
    # H1 FIX — bands recalibrated to the REAL VRP distribution. Historical S&P VRP averages
    # ~4.2pp (1990–2018) and ~6.5pp since 2020 (Cboe/CAIA); it essentially never reaches the old
    # 10/20/30pp thresholds, so this 30-point component was permanently pinned at its 5-pt floor.
    # New bands reward the realistic 2–10pp range where the premium-selling edge actually lives.
    if vrp_pct < 0:
        # Negative VRP = options cheap = no edge for sellers
        breakdown["vrp"] = 0
        disqualification_reason = f"Negative VRP ({vrp_pct:.1f}pp) — options underpriced, no seller edge"
    elif vrp_pct >= 10:
        breakdown["vrp"] = 30
    elif vrp_pct >= 6:
        breakdown["vrp"] = 27
    elif vrp_pct >= 4:
        breakdown["vrp"] = 22
    elif vrp_pct >= 2:
        breakdown["vrp"] = 15
    else:
        breakdown["vrp"] = 8   # 0–2pp: thin but positive premium

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

    # ── Technical Score Component (20 points max) ──
    breakdown["technical"] = round(min(technical_score * 0.20, 20))

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

    # ── Earnings Safety Component (5 points max) ──
    if earnings_days_away < config.EARNINGS_BLACKOUT_DAYS:
        breakdown["earnings_safety"] = 0
        if disqualification_reason is None:
            disqualification_reason = (
                f"Earnings in {earnings_days_away} days — "
                f"within {config.EARNINGS_BLACKOUT_DAYS}-day blackout window"
            )
    elif earnings_days_away <= 14:
        breakdown["earnings_safety"] = 1
    elif earnings_days_away <= 30:
        breakdown["earnings_safety"] = 3
    else:
        breakdown["earnings_safety"] = 5

    # ── Fundamentals Component (weight configurable, default 10) ──
    fundamentals_weight = int(getattr(config, "FUNDAMENTALS_WEIGHT", 10))
    fundamentals_weight = max(0, min(10, fundamentals_weight))
    if fundamentals_score is None:
        # Neutral default when no fundamentals signal is available.
        breakdown["fundamentals"] = round(fundamentals_weight * 0.5)
    else:
        normalized = max(0.0, min(10.0, float(fundamentals_score))) / 10.0
        breakdown["fundamentals"] = round(normalized * fundamentals_weight)

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


def calculate_spread_metrics(
    short_put: dict,
    long_put_strike: float,
    current_price: float,
    long_put_mid: Optional[float] = None,
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

    # Prefer the actual long-leg mid when available.
    # Fall back to a conservative estimate only if the quote is missing.
    if long_put_mid is None:
        long_put_mid = short_mid * 0.30
    credit_per_share = round(short_mid - long_put_mid, 2)
    credit_usd = round(credit_per_share * 100, 2)  # per contract

    spread_invalid = credit_per_share >= spread_width
    if spread_invalid:
        max_loss_per_share = 0.0
        max_loss_usd = 0.0
    else:
        max_loss_per_share = spread_width - credit_per_share
        max_loss_usd = round(max_loss_per_share * 100, 2)

    # Per-tier position sizing — three risk tiers, account-size agnostic
    risk_tiers = []
    for tier in getattr(config, "RISK_TIERS", []):
        tier_max = tier["max_risk"]
        viable = max_loss_usd > 0 and max_loss_usd <= tier_max
        contracts = max(1, int(tier_max / max_loss_usd)) if viable else 0
        risk_tiers.append({
            "label":          tier["label"],
            "max_risk":       tier_max,
            "contracts":      contracts,
            "max_loss_total": round(max_loss_usd * contracts, 2) if viable else 0,
            "credit_total":   round(credit_usd * contracts, 2) if viable else 0,
            "viable":         viable,
        })
    # Backward-compat: smallest viable tier's contract count, or 1
    viable_tiers = [t for t in risk_tiers if t["viable"]]
    contracts_allowed = viable_tiers[0]["contracts"] if viable_tiers else config.MIN_CONTRACTS

    profit_target_usd = round(credit_usd * config.TARGET_PROFIT_PCT, 2)
    stop_loss_usd = round(credit_usd * config.STOP_LOSS_MULTIPLIER, 2)

    strike_distance_usd = round(current_price - short_strike, 2)
    strike_distance_pct = round(strike_distance_usd / current_price * 100, 2)

    return {
        "credit_per_share": credit_per_share,
        "credit_usd": credit_usd,
        "spread_width": spread_width,
        "max_loss_usd": max_loss_usd,
        "spread_invalid": spread_invalid,
        "contracts_allowed": contracts_allowed,
        "risk_tiers": risk_tiers,
        "profit_target_usd": profit_target_usd,
        "stop_loss_close_price": round(credit_per_share * (1 + config.STOP_LOSS_MULTIPLIER), 2),
        "stop_loss_usd": stop_loss_usd,
        "strike_distance_usd": strike_distance_usd,
        "strike_distance_pct": strike_distance_pct,
    }
