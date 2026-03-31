"""
output/renderer.py - Generates the HTML tip sheet and opens it in the browser.

Uses Jinja2 templates from output/templates/.
Saves to OUTPUT_DIR from config.py.
Auto-opens in browser if AUTO_OPEN_BROWSER is True.
"""

import json
import logging
import os
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

import pytz
from jinja2 import Environment, FileSystemLoader, select_autoescape

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
BASE_DIR = Path(__file__).parent.parent


def _get_output_path(session_type: str, dt: datetime) -> Path:
    """Return the full path for the tip sheet HTML file."""
    output_dir = BASE_DIR / config.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = dt.strftime("%Y-%m-%d")
    filename = f"tipsheet_{session_type.upper()}_{date_str}.html"
    return output_dir / filename


def _build_template_context(
    session_type: str,
    qualified_trades: List[Dict],
    avoided_tickers: List[Dict],
    market_context: Dict,
    synthesis: Dict,
    account_balance: float,
    scan_timestamp: datetime,
    eod_setups: Optional[List[Dict]] = None,
    weekly_summary: Optional[Dict] = None,
    morning_signals: Optional[List[Dict]] = None,
    decay_alerts: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Build the full Jinja2 template context dict."""

    et_tz = pytz.timezone("US/Eastern")
    if scan_timestamp.tzinfo is None:
        scan_timestamp = pytz.utc.localize(scan_timestamp).astimezone(et_tz)
    else:
        scan_timestamp = scan_timestamp.astimezone(et_tz)

    timestamp_str = scan_timestamp.strftime("%B %d, %Y  %I:%M %p ET")
    date_str = scan_timestamp.strftime("%Y-%m-%d")

    # Merge synthesis into trades
    synthesis_trade_map = {
        t.get("ticker", "").upper(): t
        for t in synthesis.get("trades", [])
    }

    merged_trades = []
    for trade in qualified_trades:
        ticker = trade.get("ticker", "").upper()
        synth = synthesis_trade_map.get(ticker, {})

        merged = dict(trade)
        merged["recommendation"] = synth.get("recommendation") or "WATCH"
        merged["reasoning"] = synth.get("reasoning") or trade.get("auto_reasoning", "")
        merged["entry_instruction"] = synth.get("entry_instruction") or trade.get("entry_instruction", "")
        merged["exit_instruction"] = synth.get("exit_instruction") or trade.get("exit_instruction", "")
        merged["invalidation"] = synth.get("invalidation") or trade.get("invalidation", "")
        merged["ai_confidence"] = synth.get("confidence", trade.get("edge_score", 0))

        # Color classes for recommendation
        rec = merged["recommendation"]
        merged["rec_class"] = (
            "rec-consider" if rec == "CONSIDER"
            else "rec-watch" if rec == "WATCH"
            else "rec-avoid"
        )

        # Edge score color class
        score = merged.get("edge_score", 0)
        merged["score_class"] = (
            "score-high" if score >= 80
            else "score-med" if score >= 65
            else "score-low"
        )

        # Format percentages for display
        merged["true_pop_pct"] = round(merged.get("true_pop", 0) * 100, 1)
        merged["implied_pop_pct"] = round(merged.get("implied_pop", 0) * 100, 1)
        merged["edge_pts_display"] = (
            f"+{merged.get('edge_points', 0):.1f}"
            if merged.get("edge_points", 0) >= 0
            else f"{merged.get('edge_points', 0):.1f}"
        )

        merged_trades.append(merged)

    # Sort: CONSIDER > WATCH > AVOID, then by edge score
    order = {"CONSIDER": 0, "WATCH": 1, "AVOID": 2}
    merged_trades.sort(key=lambda t: (order.get(t.get("recommendation", "WATCH"), 1), -t.get("edge_score", 0)))

    vix = market_context.get("vix", {})
    spy = market_context.get("spy", {})

    return {
        "session_type": session_type.upper(),
        "timestamp": timestamp_str,
        "date_str": date_str,
        "account_balance": account_balance,
        "max_risk_per_trade": config.MAX_RISK_PER_TRADE_USD,
        "disclaimer": config.DISCLAIMER,
        # Market context
        "vix": vix,
        "spy": spy,
        "market_bias": synthesis.get("overall_bias", market_context.get("bias", "NEUTRAL")),
        "market_summary": synthesis.get("market_summary", market_context.get("summary", "")),
        "macro_events": market_context.get("macro_events", []),
        "session_notes": synthesis.get("session_notes", ""),
        "synthesis_source": synthesis.get("source", "unknown"),
        # Trades
        "qualified_trades": merged_trades,
        "avoided_tickers": avoided_tickers,
        # Close-session extras
        "eod_setups": eod_setups or [],
        "weekly_summary": weekly_summary,
        "morning_signals": morning_signals or [],
        "decay_alerts": decay_alerts or [],
        "is_close_session": session_type.upper() == "CLOSE",
        "is_friday": scan_timestamp.weekday() == 4,
        # Config passthrough
        "target_profit_pct": int(config.TARGET_PROFIT_PCT * 100),
        "stop_loss_multiplier": config.STOP_LOSS_MULTIPLIER,
    }


def render(
    session_type: str,
    qualified_trades: List[Dict],
    avoided_tickers: List[Dict],
    market_context: Dict,
    synthesis: Dict,
    account_balance: float,
    scan_timestamp: Optional[datetime] = None,
    eod_setups: Optional[List[Dict]] = None,
    weekly_summary: Optional[Dict] = None,
    morning_signals: Optional[List[Dict]] = None,
    decay_alerts: Optional[List[Dict]] = None,
) -> Path:
    """
    Render the tip sheet HTML and save to disk.

    Returns the path to the generated file.
    Opens in browser if AUTO_OPEN_BROWSER is True.
    """
    if scan_timestamp is None:
        scan_timestamp = datetime.utcnow()

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )

    template_name = "close.html" if session_type.upper() == "CLOSE" else "morning.html"

    try:
        template = env.get_template(template_name)
    except Exception as e:
        logger.error(f"[renderer] Template error: {e}")
        raise

    context = _build_template_context(
        session_type=session_type,
        qualified_trades=qualified_trades,
        avoided_tickers=avoided_tickers,
        market_context=market_context,
        synthesis=synthesis,
        account_balance=account_balance,
        scan_timestamp=scan_timestamp,
        eod_setups=eod_setups,
        weekly_summary=weekly_summary,
        morning_signals=morning_signals,
        decay_alerts=decay_alerts,
    )

    html = template.render(**context)

    output_path = _get_output_path(session_type, scan_timestamp)
    output_path.write_text(html, encoding="utf-8")
    logger.info(f"[renderer] Tip sheet saved: {output_path}")

    if config.AUTO_OPEN_BROWSER:
        try:
            webbrowser.open(output_path.as_uri())
            logger.info("[renderer] Opened in browser")
        except Exception as e:
            logger.warning(f"[renderer] Could not open browser: {e}")

    return output_path
