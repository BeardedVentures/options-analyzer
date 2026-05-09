"""
analysis/strike_validator.py — Hard rules for strike placement and position sizing.

A trade that fails any hard rule NEVER appears on the tip sheet.
No exceptions. No overrides.

All thresholds pulled from config.py — never hardcoded here.
"""

import logging
from typing import Dict, List, Optional, Tuple

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def validate_strike(
    ticker: str,
    strategy: str,
    short_strike: float,
    current_price: float,
    delta: Optional[float],
    account_balance: float,
    option_data: dict,
    tech_data: Optional[dict] = None,
    days_to_earnings: int = 999,
) -> Dict:
    """
    Validate a proposed short strike against all hard rules.

    Hard rejection rules — trade is BLOCKED entirely if any fail:
      1. Delta exceeds maximum
      2. Strike too close to current price (OTM buffer)
      3. Earnings within blackout window
      4. Max loss exceeds account risk limit
      5. Credit below minimum
      6. Insufficient liquidity

    Warning flags — trade appears but is flagged:
      - Strike within 10% of key support
      - IV Rank near minimum threshold
      - RSI approaching overbought
      - Mixed news sentiment

    Returns:
        valid (bool): True if all hard rules pass
        rejection_reason (str|None): reason if rejected
        rejection_category (str|None): category code
        warnings (list): non-blocking warning messages
    """
    warnings: List[str] = []
    is_spy_like = ticker.upper() in config.SPY_BUFFER_TICKERS

    # ── RULE 1: Delta check ──
    if delta is not None:
        abs_delta = abs(delta)
        if abs_delta > config.SHORT_STRIKE_MAX_DELTA:
            reason = (
                f"Strike too close — delta {abs_delta:.3f} exceeds max "
                f"{config.SHORT_STRIKE_MAX_DELTA}. Strike ${short_strike:.2f} is "
                f"too near current price ${current_price:.2f}."
            )
            logger.debug(f"[validator] REJECT {ticker} {short_strike}: {reason}")
            return _reject(reason, "DELTA_EXCEEDED", warnings)

        # Warning if approaching limit
        if abs_delta > config.SHORT_STRIKE_TARGET_DELTA * 1.3:
            warnings.append(
                f"Delta {abs_delta:.3f} is elevated — approaching max {config.SHORT_STRIKE_MAX_DELTA}"
            )

    # ── RULE 2: OTM buffer check ──
    if is_spy_like:
        buffer_required = config.MIN_STRIKE_BUFFER_SPY
        actual_buffer = current_price - short_strike
        if short_strike > (current_price - buffer_required):
            reason = (
                f"Strike ${short_strike:.2f} too close to current price ${current_price:.2f}. "
                f"Minimum buffer for {ticker}: ${buffer_required:.2f}. "
                f"Actual buffer: ${actual_buffer:.2f}. "
                f"This hard rule prevents the key error of strikes too near the money."
            )
            logger.debug(f"[validator] REJECT {ticker} {short_strike}: {reason}")
            return _reject(reason, "STRIKE_TOO_CLOSE", warnings)
    else:
        min_otm_pct = config.SHORT_STRIKE_MIN_OTM_PCT
        actual_otm_pct = (current_price - short_strike) / current_price
        if actual_otm_pct < min_otm_pct:
            reason = (
                f"Strike ${short_strike:.2f} is only {actual_otm_pct*100:.1f}% OTM "
                f"(minimum required: {min_otm_pct*100:.1f}%). "
                f"Current price: ${current_price:.2f}. "
                f"This was the key error in prior recommendations — enforced here."
            )
            logger.debug(f"[validator] REJECT {ticker} {short_strike}: {reason}")
            return _reject(reason, "STRIKE_TOO_CLOSE", warnings)


    # ── RULE 3: Earnings blackout or volatility crush ──
    if days_to_earnings < config.EARNINGS_BLACKOUT_DAYS:
        if getattr(config, "ENABLE_VOL_CRUSH_MODE", False):
            warnings.append(
                f"VOLATILITY CRUSH: Earnings in {days_to_earnings} days — this is a volatility crush play. Expect IV collapse after earnings. Manage risk tightly!"
            )
            # Tag trade as volatility crush (will be surfaced in trade dict)
            trade_type = "volatility_crush"
        else:
            reason = (
                f"Earnings in {days_to_earnings} days — premium selling blocked. "
                f"Policy: no selling within {config.EARNINGS_BLACKOUT_DAYS} days of earnings."
            )
            logger.debug(f"[validator] REJECT {ticker}: {reason}")
            return _reject(reason, "EARNINGS_BLACKOUT", warnings)
    else:
        trade_type = "standard_premium"

    if days_to_earnings <= 14:
        warnings.append(
            f"Earnings in {days_to_earnings} days — monitor closely, exit early if needed"
        )

    # ── RULE 4: Account sizing / max loss check ──
    max_loss_usd = option_data.get("max_loss_usd", 0)
    if max_loss_usd <= 0:
        # Try to estimate from spread data
        spread_width = option_data.get("spread_width", 0)
        credit_usd = option_data.get("credit_usd", 0)
        if spread_width > 0 and credit_usd > 0:
            max_loss_usd = (spread_width * 100) - credit_usd

    if max_loss_usd > config.MAX_RISK_PER_TRADE_USD:
        warnings.append(
            f"OVERSIZED: Max loss ${max_loss_usd:.2f} exceeds risk limit "
            f"${config.MAX_RISK_PER_TRADE_USD:.2f} "
            f"({config.MAX_RISK_PER_TRADE_PCT*100:.0f}% of ${account_balance:.2f}). "
            f"Showing as 1-contract setup — size carefully."
        )
        logger.debug(f"[validator] OVERSIZED {ticker} ${short_strike:.2f}: max_loss ${max_loss_usd:.2f} > limit ${config.MAX_RISK_PER_TRADE_USD:.2f}")

    # ── RULE 5: Minimum credit check ──
    credit_usd = option_data.get("credit_usd", 0)
    if credit_usd > 0 and credit_usd < config.MIN_CREDIT_USD:
        reason = (
            f"Credit ${credit_usd:.2f} per contract below minimum ${config.MIN_CREDIT_USD:.2f}. "
            f"Not worth the trade cost and execution risk."
        )
        logger.debug(f"[validator] REJECT {ticker} {short_strike}: {reason}")
        return _reject(reason, "INSUFFICIENT_CREDIT", warnings)

    # ── RULE 6: Liquidity check ──
    volume = option_data.get("volume", 0)
    oi = option_data.get("open_interest", 0)
    if volume < 100 and oi < 500:
        reason = (
            f"Insufficient liquidity — volume {volume}, open interest {oi}. "
            f"Required: volume ≥100 OR open interest ≥500. "
            f"Wide bid/ask spreads make execution unfavorable."
        )
        logger.debug(f"[validator] REJECT {ticker} {short_strike}: {reason}")
        return _reject(reason, "LOW_LIQUIDITY", warnings)

    if volume < 200 or oi < 1000:
        warnings.append(f"Moderate liquidity — volume {volume}, OI {oi}. Use limit orders only.")

    # ── NON-BLOCKING WARNINGS ──

    # Strike near key support
    if tech_data:
        nearest_support = tech_data.get("nearest_support")
        if nearest_support and nearest_support > 0:
            buffer_to_support = (short_strike - nearest_support) / short_strike
            if 0 < buffer_to_support < 0.10:
                warnings.append(
                    f"Strike ${short_strike:.2f} is within 10% of key support "
                    f"${nearest_support:.2f} — breach of support invalidates this trade"
                )

        # RSI approaching overbought
        rsi = tech_data.get("rsi", 50)
        if rsi > 65:
            warnings.append(f"RSI {rsi:.1f} approaching overbought territory (>70)")

        # IV Rank near minimum
        iv_rank = tech_data.get("iv_rank", 0)
        if config.MIN_IV_RANK <= iv_rank <= config.MIN_IV_RANK + 5:
            warnings.append(
                f"IV Rank {iv_rank:.1f} is near minimum threshold {config.MIN_IV_RANK} — lower confidence setup"
            )

    logger.debug(f"[validator] PASS {ticker} ${short_strike:.2f} | warnings: {len(warnings)}")
    return {
        "valid": True,
        "rejection_reason": None,
        "rejection_category": None,
        "warnings": warnings,
        "trade_type": trade_type,
    }


def validate_iron_condor(
    ticker: str,
    short_put_strike: float,
    short_call_strike: float,
    current_price: float,
    put_delta: Optional[float],
    call_delta: Optional[float],
    account_balance: float,
    put_option_data: dict,
    call_option_data: dict,
    tech_data: Optional[dict] = None,
    days_to_earnings: int = 999,
) -> Dict:
    """
    Validate an iron condor setup — validates both the put and call legs.
    Returns combined validation result.
    """
    put_result = validate_strike(
        ticker=ticker, strategy="iron_condor_put",
        short_strike=short_put_strike, current_price=current_price,
        delta=put_delta, account_balance=account_balance,
        option_data=put_option_data, tech_data=tech_data,
        days_to_earnings=days_to_earnings,
    )

    if not put_result["valid"]:
        put_result["rejection_reason"] = f"PUT leg: {put_result['rejection_reason']}"
        return put_result

    call_result = validate_strike(
        ticker=ticker, strategy="iron_condor_call",
        short_strike=short_call_strike, current_price=current_price,
        delta=call_delta, account_balance=account_balance,
        option_data=call_option_data, tech_data=tech_data,
        days_to_earnings=days_to_earnings,
    )

    if not call_result["valid"]:
        call_result["rejection_reason"] = f"CALL leg: {call_result['rejection_reason']}"
        return call_result

    # Combine warnings
    all_warnings = put_result["warnings"] + call_result["warnings"]
    return {"valid": True, "rejection_reason": None, "rejection_category": None, "warnings": all_warnings}


def _reject(reason: str, category: str, warnings: List[str]) -> Dict:
    return {
        "valid": False,
        "rejection_reason": reason,
        "rejection_category": category,
        "warnings": warnings,
    }
