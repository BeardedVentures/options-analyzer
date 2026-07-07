"""
analysis/synthesizer.py — Claude API call for final tip sheet narrative.

Single API call per scan session.
If Claude API key is not set, generates structured output from raw data
without narrative — still fully functional.
"""

import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# RECOMMENDATION LOGIC (fallback — no AI)
# ─────────────────────────────────────────────

def _auto_recommendation(trade: Dict) -> str:
    """
    Determine CONSIDER / WATCH / AVOID based on edge score and warnings.
    Used when Claude API is unavailable.
    """
    score = trade.get("edge_score", 0)
    warnings = trade.get("warnings", [])

    if score >= 75 and len(warnings) == 0:
        return "CONSIDER"
    elif score >= 65 or (score >= 60 and len(warnings) <= 1):
        return "WATCH"
    else:
        return "AVOID"


def _auto_entry_instruction(trade: Dict) -> str:
    """Generate entry instruction from trade data."""
    ticker = trade.get("ticker", "")
    short_strike = trade.get("short_strike", 0)
    long_strike = trade.get("long_strike", 0)
    expiration = trade.get("expiration_display") or trade.get("last_trade_date") or trade.get("expiration", "")
    credit = trade.get("credit_per_share", 0)
    spread_width = trade.get("spread_width", 0)

    short_label = f"${short_strike:.0f}"
    long_label = f"${long_strike:.0f}"

    return (
        f"Sell {ticker} {short_label}/{long_label} put spread "
        f"expiring {expiration} at ${credit:.2f} credit (limit order). "
        f"Place order as a spread order — sell {short_label} put, buy {long_label} put simultaneously. "
        f"Good for 30-45 minutes after market open when bid/ask narrows."
    )


def _auto_exit_instruction(trade: Dict) -> str:
    """Generate exit instruction from trade data."""
    credit = trade.get("credit_per_share", 0)
    profit_target = trade.get("profit_target_price", 0)
    stop_loss = trade.get("stop_loss_close_price", 0)
    target_pct = int(config.TARGET_PROFIT_PCT * 100)

    return (
        f"Buy back spread when it decays to ${profit_target:.2f} ({target_pct}% profit target). "
        f"Stop loss: buy back if spread price reaches ${stop_loss:.2f} "
        f"({config.STOP_LOSS_MULTIPLIER:.0f}x credit received). "
        f"Set GTC limit order at profit target immediately after entry."
    )


def _auto_invalidation(trade: Dict) -> str:
    """Generate trade invalidation condition."""
    ticker = trade.get("ticker", "")
    short_strike = trade.get("short_strike", 0)
    nearest_support = trade.get("nearest_support")
    current_price = trade.get("current_price", 0)

    if nearest_support:
        level = nearest_support
        level_label = f"support at ${level:.2f}"
    else:
        level = current_price * 0.97
        level_label = f"the ${level:.2f} level (3% below entry)"

    return (
        f"{ticker} breaks below {level_label} on high volume — "
        f"exit immediately regardless of spread price."
    )


def _fallback_synthesis(
    session_type: str,
    qualified_trades: List[Dict],
    market_context: Dict,
    account_balance: float,
) -> Dict:
    """
    Generate tip sheet data without Claude API.
    Produces the same structure as the Claude-powered version.
    """
    vix = market_context.get("vix", {})
    vix_current = vix.get("current", 0)
    if vix_current is None:
        vix_current = 0.0
    vix_label = vix.get("label", "UNKNOWN")

    spy = market_context.get("spy", {})
    spy_change = spy.get("day_change_pct", 0)
    if spy_change is None:
        spy_change = 0.0

    if spy_change > 0.5 and vix_current < 20:
        bias = "RISK-ON"
    elif spy_change < -0.5 or vix_current > 25:
        bias = "RISK-OFF"
    else:
        bias = "NEUTRAL"

    market_summary = (
        f"VIX at {vix_current:.1f} ({vix_label}), "
        f"SPY {'up' if spy_change >= 0 else 'down'} {abs(spy_change):.2f}% on the day. "
        f"Market bias: {bias}. "
        f"{'Elevated volatility favors premium sellers — verify setups carefully.' if vix_current > 20 else 'Low volatility — select only the highest-edge setups.'}"
    )


    trades_output = []
    for trade in qualified_trades:
        rec = _auto_recommendation(trade)
        entry = _auto_entry_instruction(trade)
        exit_instr = _auto_exit_instruction(trade)
        invalidation = _auto_invalidation(trade)

        trade_type = trade.get("trade_type", "standard_premium")
        if trade_type == "volatility_crush":
            type_narrative = (
                "This is a VOLATILITY CRUSH play: selling premium ahead of earnings to capture IV collapse after the event. "
                "Expect rapid changes in option prices and manage risk tightly. "
                "These setups are higher risk and require active management."
            )
        else:
            type_narrative = (
                "This is a STANDARD PREMIUM SELLING setup: high-probability, high-edge trade with no earnings event risk. "
                "Focus is on steady premium decay and high win rate."
            )

        trades_output.append({
            "ticker": trade.get("ticker"),
            "trade_type": trade_type,
            "reasoning": (
                f"{type_narrative} "
                f"IV Rank {trade.get('iv_rank', 0):.0f} with VRP of "
                f"{trade.get('vrp', 0):.1f}pp — options overpriced relative to realized vol. "
                f"True historical POP {trade.get('true_pop', 0)*100:.0f}% vs "
                f"market implied {trade.get('implied_pop', 0)*100:.0f}% = "
                f"{trade.get('edge_points', 0):.1f} edge points. "
                f"Technical trend: {trade.get('trend', 'NEUTRAL')}. "
                f"Edge score: {trade.get('edge_score', 0)}/100."
            ),
            "recommendation": rec,
            "entry_instruction": entry,
            "exit_instruction": exit_instr,
            "invalidation": invalidation,
            "confidence": trade.get("edge_score", 0),
            "source": "rule_based",
        })

    return {
        "market_summary": market_summary,
        "overall_bias": bias,
        "trades": trades_output,
        "source": "fallback_no_api",
    }


# ─────────────────────────────────────────────
# CLAUDE API SYNTHESIS
# ─────────────────────────────────────────────

def synthesize_tipsheet(
    session_type: str,
    qualified_trades: List[Dict],
    market_context: Dict,
    account_balance: float,
    scan_timestamp: str,
) -> Dict:
    """
    Call Claude API to generate narrative analysis for the tip sheet.

    Single API call per scan session. All trades passed in one batch.
    Falls back to rule-based synthesis if API key not configured.

    Returns structured dict matching the tip sheet template schema.
    """
    if not config.ANTHROPIC_API_KEY:
        logger.info("[synthesizer] No Anthropic API key — using fallback synthesis")
        return _fallback_synthesis(session_type, qualified_trades, market_context, account_balance)

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Prepare trade data — redact raw data, keep key fields
        trades_for_claude = []
        for t in qualified_trades:
            trades_for_claude.append({
                "ticker": t.get("ticker"),
                "strategy": t.get("strategy"),
                "short_strike": t.get("short_strike"),
                "long_strike": t.get("long_strike"),
                "expiration": t.get("expiration"),
                "last_trade_date": t.get("last_trade_date"),
                "expiration_display": t.get("expiration_display") or t.get("last_trade_date") or t.get("expiration"),
                "dte": t.get("dte"),
                "current_price": t.get("current_price"),
                "credit_per_share": t.get("credit_per_share"),
                "credit_usd": t.get("credit_usd"),
                "max_loss_usd": t.get("max_loss_usd"),
                "true_pop_pct": round(t.get("true_pop", 0) * 100, 1),
                "implied_pop_pct": round(t.get("implied_pop", 0) * 100, 1),
                "edge_points": t.get("edge_points"),
                "edge_score": t.get("edge_score"),
                "delta": t.get("delta"),
                "iv_rank": t.get("iv_rank"),
                "vrp_pct": t.get("vrp"),
                "trend": t.get("trend"),
                "rsi": t.get("rsi"),
                "nearest_support": t.get("nearest_support"),
                "news_sentiment": t.get("news_sentiment"),
                "news_summary": t.get("news_summary"),
                "warnings": t.get("warnings", []),
                "profit_target_price": t.get("profit_target_price"),
                "stop_loss_close_price": t.get("stop_loss_close_price"),
            })

        vix = market_context.get("vix", {})

        system_prompt = f"""You are a professional options trading analyst generating a {session_type} tip sheet for a retail trader with a ${account_balance:.0f} account.

You have been provided pre-screened trade setups that ALREADY PASSED all quantitative filters. These are BULL PUT SPREAD setups only. Your job:

1. Write clear, direct reasoning for each trade (2-3 sentences max per trade)
2. Summarize current market conditions in one paragraph
3. Flag any conflicts between technical signals and news
4. Assign final CONSIDER / WATCH / AVOID for each trade
5. Write a specific entry instruction (limit order price, timing, execution method)
6. Write a specific exit instruction (profit target price, stop loss price with specific levels)
7. Write ONE sentence on what would invalidate this trade
{"8. For the CLOSE session: add any overnight/next-day mean reversion observation if relevant" if session_type == "CLOSE" else ""}

TONE: Direct, professional. No hedging. Treat the trader as intelligent. Be specific with prices.
FORMAT: Return ONLY valid JSON — no markdown, no code blocks, no explanation outside the JSON.

DISCLAIMER: Your output is for educational purposes only. Not financial advice.

Return this exact JSON structure:
{{
  "market_summary": "string",
  "overall_bias": "RISK-ON" | "RISK-OFF" | "NEUTRAL",
  "trades": [
    {{
      "ticker": "string",
      "reasoning": "string",
      "recommendation": "CONSIDER" | "WATCH" | "AVOID",
      "entry_instruction": "string",
      "exit_instruction": "string",
      "invalidation": "string",
      "confidence": 0-100
    }}
  ],
  "session_notes": "string (optional — one overall observation)"
}}"""

        user_content = (
            f"Session: {session_type} | Timestamp: {scan_timestamp} | "
            f"Account: ${account_balance:.2f}\n\n"
            f"Market Context:\n{json.dumps(market_context, indent=2, default=str)}\n\n"
            f"Qualified Trades ({len(trades_for_claude)}):\n"
            f"{json.dumps(trades_for_claude, indent=2, default=str)}\n\n"
            f"Generate the tip sheet JSON now."
        )

        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=3000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

        raw = response.content[0].text.strip()
        logger.debug(f"[synthesizer] Claude response length: {len(raw)} chars")

        # Extract JSON
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            result["source"] = "claude_api"
            logger.info(f"[synthesizer] Claude synthesis complete — {len(result.get('trades', []))} trades")
            return result

        logger.warning("[synthesizer] Could not parse Claude response as JSON — using fallback")
        fallback = _fallback_synthesis(session_type, qualified_trades, market_context, account_balance)
        fallback["source"] = "claude_parse_error"
        return fallback

    except Exception as e:
        msg = str(e)
        if "credit balance is too low" in msg.lower():
            logger.warning("[synthesizer] Claude credits unavailable — using fallback synthesis")
        else:
            logger.error(f"[synthesizer] Claude API error: {e}")
        fallback = _fallback_synthesis(session_type, qualified_trades, market_context, account_balance)
        fallback["source"] = f"claude_api_error: {type(e).__name__}"
        return fallback
