"""
bv_content_formatter.py — BeardedVentures Social Media Content Generator

Reads a WOLF scan result and produces ready-to-review social media posts
for the BeardedVentures trading channel.

TWO INPUT MODES:
  1. Scan log (lightweight) — from options_intelligence/logs/scan_log.json
     Gives ticker names + edge scores + rejection reasons. Good for quick posts.

  2. VEGA payload (full) — the same JSON body sent to JARVIS /vega/ingest
     Includes full trade details: IV rank, VRP, strikes, credit, POP, trend, etc.
     Produces richer, more specific content.

OUTPUT:
  - JSON file: bv_content_YYYY-MM-DD_SESSION.json  (machine-readable, for n8n)
  - TXT file:  bv_content_YYYY-MM-DD_SESSION.txt   (human-readable, for review)

USAGE:
  # Use latest entry from scan_log.json (quick mode):
  python bv_content_formatter.py

  # Use a specific scan log entry (by index, 0 = oldest, -1 = latest):
  python bv_content_formatter.py --scan-log logs/scan_log.json --index -1

  # Use a full VEGA payload JSON file:
  python bv_content_formatter.py --vega-file my_vega_payload.json

  # Pipe a VEGA payload from stdin:
  echo '{"session_type": "morning", ...}' | python bv_content_formatter.py --stdin

  # Fetch latest scan from JARVIS tower:
  python bv_content_formatter.py --from-jarvis --jarvis-host http://192.168.0.222:8000

  # Output to a specific folder:
  python bv_content_formatter.py --output-dir output/social_content

DISCLAIMER:
  All generated content includes mandatory disclaimer. Posts are educational only.
  Never remove the disclaimer before posting.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

DISCLAIMER = (
    "⚠️ Educational only. Not financial advice. "
    "Options trading involves significant risk of loss. "
    "All decisions are yours. Past results ≠ future performance."
)

DISCLAIMER_SHORT = "Not financial advice. Educational only. #OptionsTrading"

# ─────────────────────────────────────────────
# CONTENT TEMPLATES
# ─────────────────────────────────────────────

def _bias_emoji(bias: str) -> str:
    return {"RISK-ON": "🟢", "RISK-OFF": "🔴", "NEUTRAL": "🟡"}.get(bias.upper(), "⚪")


def _vix_context(vix_level: Optional[float], vix_label: Optional[str]) -> str:
    if vix_level is None:
        return "VIX data unavailable"
    label = vix_label or ("elevated" if vix_level > 20 else "low")
    if vix_level > 30:
        return f"VIX at {vix_level:.0f} — fear spike. Premium sellers win in chaos."
    elif vix_level > 20:
        return f"VIX at {vix_level:.0f} ({label}) — elevated IV, options are juicy."
    elif vix_level > 15:
        return f"VIX at {vix_level:.0f} — moderate. Be selective."
    else:
        return f"VIX at {vix_level:.0f} — compressed. Only highest-edge setups qualify."


def _session_label(session_type: str) -> str:
    return "Morning" if session_type.lower() == "morning" else "Close"


def _format_edge_score(score: int) -> str:
    if score >= 80:
        return f"{score}/100 🔥"
    elif score >= 70:
        return f"{score}/100 ✅"
    elif score >= 60:
        return f"{score}/100 👀"
    else:
        return f"{score}/100"


def _rejection_insight(rejected_trades: List[Dict]) -> Optional[str]:
    """Pull the most interesting rejection reason for educational content."""
    category_map = {}
    for r in rejected_trades:
        cat = r.get("category", "UNKNOWN")
        category_map[cat] = category_map.get(cat, 0) + 1

    if not category_map:
        return None

    top_cat = max(category_map, key=category_map.get)
    count = category_map[top_cat]

    insights = {
        "IV_RANK": f"{count} ticker(s) filtered out — IV Rank too low. Low IV = options are cheap = no seller edge. Wait for elevated vol.",
        "EDGE_SCORE": f"{count} ticker(s) cut on edge score. We only trade when probability + premium + technicals all align.",
        "LOW_LIQUIDITY": f"{count} ticker(s) had liquidity issues — wide bid/ask spreads eat your profit before you even start.",
        "NO_STRIKE": f"{count} ticker(s) had no strike meeting delta + OTM requirements. We don't force trades that don't fit.",
        "MIN_POP": f"{count} ticker(s) below min probability of profit. We require 72%+ true historical POP — not the delta shortcut.",
        "NEWS_BLOCK": f"{count} ticker(s) blocked by negative news. Earnings surprises and BLOCKING events = stay flat.",
        "EDGE_SCORE": f"{count} setup(s) cut — VRP was negative. Implied vol below realized vol means options are underpriced. No edge for sellers.",
    }
    return insights.get(top_cat, f"{count} ticker(s) filtered — {top_cat.lower().replace('_', ' ')}")


# ─────────────────────────────────────────────
# POST GENERATORS
# ─────────────────────────────────────────────

def generate_market_context_post(scan: Dict) -> Dict:
    """
    Post 1 — Market conditions update.
    Works from lightweight scan_log or full VEGA payload.
    Twitter/X: ~240 chars. LinkedIn: full version.
    """
    session_type = scan.get("session_type", "morning")
    session = _session_label(session_type)
    timestamp = scan.get("timestamp", "")
    bias = scan.get("market_bias") or scan.get("bias", "NEUTRAL")
    vix_level = scan.get("vix_level")
    vix_label = scan.get("vix_label")
    spy_change = scan.get("spy_change_pct")
    tickers_scanned = len(scan.get("tickers_scanned", []))
    qualified = scan.get("qualified_trades", [])
    n_qualified = len(qualified)

    # Parse date for display
    try:
        dt = datetime.fromisoformat(timestamp)
        date_display = dt.strftime("%B %d")
    except Exception:
        date_display = "Today"

    spy_txt = ""
    if spy_change is not None:
        direction = "up" if spy_change >= 0 else "down"
        spy_txt = f"SPY {direction} {abs(spy_change):.1f}%. "

    vix_txt = _vix_context(vix_level, vix_label)
    bias_emoji = _bias_emoji(bias)

    # Short version (Twitter/X — target ~240 chars)
    short = (
        f"{bias_emoji} WOLF {session} Scan | {date_display}\n\n"
        f"{spy_txt}{vix_txt}\n\n"
        f"Scanned {tickers_scanned} tickers → {n_qualified} qualified setup(s).\n\n"
        f"{DISCLAIMER_SHORT}"
    )

    # Long version (LinkedIn/Facebook)
    top_trade_line = ""
    if n_qualified > 0 and isinstance(qualified[0], dict) and "ticker" in qualified[0]:
        top = qualified[0]
        score = top.get("edge_score", 0)
        top_trade_line = f"\nTop qualifier: ${top['ticker']} — Edge Score {_format_edge_score(score)}\n"

    rejection_note = ""
    rejected = scan.get("rejected_trades", [])
    if rejected:
        insight = _rejection_insight(rejected)
        if insight:
            rejection_note = f"\n📊 Filter insight: {insight}\n"

    long = (
        f"{bias_emoji} WOLF {session} Scan — {date_display}\n\n"
        f"Market conditions:\n"
        f"• {spy_txt.strip() or 'SPY data pending'}\n"
        f"• {vix_txt}\n"
        f"• Bias: {bias}\n\n"
        f"Scan results:\n"
        f"• {tickers_scanned} tickers screened\n"
        f"• {n_qualified} setup(s) cleared all filters\n"
        f"• {len(rejected)} rejected by quantitative rules\n"
        f"{top_trade_line}"
        f"{rejection_note}\n"
        f"The scanner runs every market day — morning & close. "
        f"Every setup must clear IV rank, VRP edge, true probability of profit, "
        f"technicals, and news sentiment before appearing on the sheet.\n\n"
        f"{DISCLAIMER}"
    )

    return {
        "post_type": "market_context",
        "session": session_type,
        "date": date_display,
        "platforms": {
            "twitter": short,
            "linkedin": long,
        },
        "char_count_twitter": len(short),
    }


def generate_setup_highlight_post(scan: Dict) -> Optional[Dict]:
    """
    Post 2 — Top qualified setup highlight.
    Only generated when there is at least one qualified trade.
    Uses full trade detail if available (VEGA payload), falls back to
    ticker + edge_score if only scan_log data is present.
    """
    qualified = scan.get("qualified_trades", [])
    if not qualified:
        return None

    # Sort by edge_score descending
    try:
        qualified_sorted = sorted(qualified, key=lambda t: t.get("edge_score", 0), reverse=True)
    except Exception:
        qualified_sorted = qualified

    top = qualified_sorted[0]
    ticker = top.get("ticker", "???")
    edge_score = top.get("edge_score", 0)

    timestamp = scan.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(timestamp)
        date_display = dt.strftime("%B %d")
    except Exception:
        date_display = "Today"

    # Check if we have full trade detail (VEGA payload) or just lightweight summary
    has_full_detail = any(k in top for k in ("iv_rank", "short_strike", "credit_per_share", "true_pop"))

    if has_full_detail:
        # Rich post from full VEGA trade data
        strategy = top.get("strategy", "bull_put_spread").replace("_", " ").title()
        current_price = top.get("current_price")
        short_strike = top.get("short_strike")
        long_strike = top.get("long_strike")
        expiration = top.get("expiration_display") or top.get("last_trade_date") or top.get("expiration", "")
        dte = top.get("dte")
        credit = top.get("credit_per_share")
        credit_usd = top.get("credit_usd")
        iv_rank = top.get("iv_rank")
        vrp = top.get("vrp")
        true_pop = top.get("true_pop")
        implied_pop = top.get("implied_pop")
        edge_points = top.get("edge_points")
        trend = top.get("trend", "NEUTRAL")
        rsi = top.get("rsi")
        recommendation = top.get("recommendation", "WATCH")

        rec_emoji = {"CONSIDER": "✅", "WATCH": "👀", "AVOID": "🚫"}.get(recommendation, "📋")

        spread_line = ""
        if short_strike and long_strike:
            spread_line = f"${short_strike:.0f}/{long_strike:.0f} put spread"
        elif short_strike:
            spread_line = f"${short_strike:.0f} put"

        credit_line = ""
        if credit:
            credit_line = f"${credit:.2f}/share"
            if credit_usd:
                credit_line += f" (${credit_usd:.0f}/contract)"

        pop_line = ""
        if true_pop and implied_pop:
            edge_pts_str = f"+{edge_points:.1f}" if edge_points and edge_points >= 0 else f"{edge_points:.1f}" if edge_points else ""
            pop_line = (
                f"True POP: {true_pop*100:.0f}% vs implied {implied_pop*100:.0f}% "
                f"= {edge_pts_str} edge pts"
            )

        short = (
            f"📋 WOLF Setup | {ticker} — {date_display}\n\n"
            f"{rec_emoji} {recommendation} | Edge {_format_edge_score(edge_score)}\n"
            f"{strategy} {spread_line}\n"
            f"Credit: {credit_line}\n"
            f"IV Rank: {iv_rank:.0f} | VRP: +{vrp:.1f}%\n\n"
            f"{DISCLAIMER_SHORT}"
        )

        long = (
            f"📋 WOLF Setup Highlight — {ticker} | {date_display}\n\n"
            f"{rec_emoji} Recommendation: {recommendation}\n"
            f"Strategy: {strategy}\n"
            f"Structure: {spread_line} exp {expiration} ({dte}d)\n"
            f"Credit collected: {credit_line}\n\n"
            f"Why this setup qualified:\n"
            f"• IV Rank {iv_rank:.0f} — elevated, options pricing in more fear than history justifies\n"
            f"• VRP +{vrp:.1f}% — implied vol exceeds realized vol (seller's edge)\n"
            f"• {pop_line}\n"
            f"• Trend: {trend} | RSI: {rsi:.0f}\n"
            f"• Edge Score: {_format_edge_score(edge_score)}\n\n"
            f"This is the kind of setup WOLF is built to find — "
            f"IV elevated above realized vol, probability edge confirmed historically, "
            f"trend aligned. Not every day has one. Today it does.\n\n"
            f"{DISCLAIMER}"
        )

    else:
        # Lightweight post — just ticker + edge score
        n_qualified = len(qualified)
        others = [t.get("ticker") for t in qualified_sorted[1:3] if t.get("ticker")]
        others_line = f" Also watching: {', '.join(others)}." if others else ""

        short = (
            f"📋 WOLF Top Setup | {date_display}\n\n"
            f"${ticker} cleared all filters — Edge Score {_format_edge_score(edge_score)}\n"
            f"{n_qualified} total qualified today.{others_line}\n\n"
            f"{DISCLAIMER_SHORT}"
        )

        long = (
            f"📋 WOLF Scan Highlight — {date_display}\n\n"
            f"Top setup today: ${ticker}\n"
            f"Edge Score: {_format_edge_score(edge_score)}\n\n"
            f"{n_qualified} setup(s) cleared all quantitative filters today "
            f"(IV rank, VRP edge, probability of profit, technicals, news sentiment).\n"
            f"{others_line}\n\n"
            f"WOLF runs on a systematic model — no gut calls. "
            f"Every ticker passes the same rules every day.\n\n"
            f"{DISCLAIMER}"
        )

    return {
        "post_type": "setup_highlight",
        "ticker": ticker,
        "session": scan.get("session_type", "morning"),
        "date": date_display,
        "platforms": {
            "twitter": short,
            "linkedin": long,
        },
        "char_count_twitter": len(short),
        "has_full_detail": has_full_detail,
    }


def generate_educational_post(scan: Dict) -> Dict:
    """
    Post 3 — Educational content derived from what the scanner filtered out.
    Rotates through different lesson angles based on the most common rejection reason.
    Works from both scan_log and VEGA payload.
    """
    rejected = scan.get("rejected_trades", [])
    timestamp = scan.get("timestamp", "")
    session_type = scan.get("session_type", "morning")

    try:
        dt = datetime.fromisoformat(timestamp)
        date_display = dt.strftime("%B %d")
        # Rotate topic based on day of week
        topic_index = dt.weekday()  # 0=Mon, 4=Fri
    except Exception:
        date_display = "Today"
        topic_index = 0

    # Find dominant rejection category
    category_map: Dict[str, List[str]] = {}
    for r in rejected:
        cat = r.get("category", "UNKNOWN")
        ticker = r.get("ticker", "")
        if cat not in category_map:
            category_map[cat] = []
        if ticker:
            category_map[cat].append(ticker)

    # Pick the most common non-trivial rejection
    priority_cats = ["IV_RANK", "EDGE_SCORE", "LOW_LIQUIDITY", "MIN_POP", "NEWS_BLOCK", "NO_STRIKE"]
    top_cat = None
    top_tickers: List[str] = []
    for cat in priority_cats:
        if cat in category_map:
            top_cat = cat
            top_tickers = category_map[cat][:3]
            break

    # Fallback: most common category
    if not top_cat and category_map:
        top_cat = max(category_map, key=lambda c: len(category_map[c]))
        top_tickers = category_map[top_cat][:3]

    # Educational content library
    lessons = {
        "IV_RANK": {
            "hook": "Why WOLF skipped {tickers} today 📊",
            "body": (
                "IV Rank was too low.\n\n"
                "IV Rank tells you where current implied volatility sits "
                "relative to the past 52 weeks (0 = lowest, 100 = highest).\n\n"
                "We require IV Rank ≥ 45 before selling premium.\n\n"
                "Why? Because we're selling options — when IV is low, "
                "the premium you collect doesn't compensate for the risk. "
                "You're selling cheap. Wait for vol to spike.\n\n"
                "Low IV = cheap options = no seller's edge.\n"
                "High IV = expensive options = premium seller's paradise."
            ),
            "cta": "What's your IV Rank threshold? Drop it below. 👇",
        },
        "EDGE_SCORE": {
            "hook": "Negative VRP killed {tickers} today 🚫",
            "body": (
                "VRP = Volatility Risk Premium.\n\n"
                "It measures how much implied volatility exceeds realized volatility. "
                "When IV > realized vol, the market is overpaying for protection — "
                "and that's where we get our edge as premium sellers.\n\n"
                "Negative VRP means the opposite: options are priced BELOW "
                "what the stock has actually moved. Selling in that environment "
                "means collecting less than fair value.\n\n"
                "WOLF requires positive VRP before any trade qualifies. "
                "No edge = no trade. Simple."
            ),
            "cta": "VRP is one of the most overlooked edges in retail options. Follow for more. 👇",
        },
        "LOW_LIQUIDITY": {
            "hook": "Liquidity killed {tickers} — here's why it matters 💧",
            "body": (
                "A wide bid/ask spread is a hidden tax on every options trade.\n\n"
                "Example: If the bid is $0.90 and the ask is $1.20, "
                "you're starting underwater the moment you fill.\n\n"
                "WOLF requires:\n"
                "• Volume ≥ 100 OR open interest ≥ 500 at the target strike\n"
                "• Reasonable bid/ask spread\n\n"
                "Retail traders lose millions every year on illiquid options. "
                "The scanner just saved you from that today."
            ),
            "cta": "Always check liquidity before entering. Slippage is real. 👇",
        },
        "MIN_POP": {
            "hook": "True POP vs Delta POP — not the same thing 📐",
            "body": (
                "Most traders use delta as a shortcut for probability of profit.\n\n"
                "Delta = 0.20 → 80% chance of profit? Not exactly.\n\n"
                "Delta is a Black-Scholes model output — it uses implied volatility, "
                "which is what the market THINKS will happen.\n\n"
                "True POP uses actual historical price distributions: "
                "how often has this stock moved past this strike over the same time period?\n\n"
                "WOLF requires True POP ≥ 72%. Several tickers today "
                "looked great on delta but failed on historical probability.\n\n"
                "The model doesn't care about narratives. Only evidence."
            ),
            "cta": "True POP > delta shortcut. Every time. 👇",
        },
        "NEWS_BLOCK": {
            "hook": "WOLF blocked {tickers} on news today 📰",
            "body": (
                "Selling premium before a major news catalyst is a coin flip — "
                "except the coin is weighted against you.\n\n"
                "WOLF scans news sentiment for every ticker on every scan. "
                "If there's a blocking event (pending earnings, FDA decision, "
                "merger announcement, or strongly negative macro signal), "
                "the ticker gets pulled regardless of how good the setup looks.\n\n"
                "Edge doesn't matter if an exogenous shock can gap you "
                "through your short strike overnight.\n\n"
                "Discipline means skipping good setups at bad times."
            ),
            "cta": "Risk management > setup quality. Follow for daily WOLF updates. 👇",
        },
        "NO_STRIKE": {
            "hook": "Why we don't force trades that don't fit 🎯",
            "body": (
                "Today WOLF couldn't find a valid strike for some tickers.\n\n"
                "Our rules:\n"
                "• Short strike must be ≥ 3% OTM from current price\n"
                "• Delta must be between 0.15 and 0.30\n"
                "• Spread width must fit within account risk parameters\n\n"
                "Sometimes the right strike just doesn't exist at the right price. "
                "Forcing a trade by loosening criteria defeats the whole system.\n\n"
                "A trading system only works if you actually follow it. "
                "No setup = no trade. Cash is a position."
            ),
            "cta": "Patience is a strategy. Cash is a position. 👇",
        },
    }

    # Default educational content if no specific rejection
    default_lesson = {
        "hook": f"How WOLF screens options every market day 🔍",
        "body": (
            "Every morning and close, WOLF runs 13 tickers through a 5-layer filter:\n\n"
            "1️⃣ IV Rank ≥ 45 — only trade elevated volatility\n"
            "2️⃣ Positive VRP — implied vol must exceed realized vol\n"
            "3️⃣ True POP ≥ 72% — historical probability, not delta shortcut\n"
            "4️⃣ Technical alignment — trend + RSI + support levels\n"
            "5️⃣ News clear — no blocking events, earnings blackout enforced\n\n"
            "Only setups that pass all 5 layers appear on the sheet. "
            "Most days, most tickers fail. That's the point."
        ),
        "cta": "Follow for daily scan results. 👇",
    }

    lesson = lessons.get(top_cat, default_lesson)

    # Format tickers string
    tickers_str = ", ".join(f"${t}" for t in top_tickers) if top_tickers else "several tickers"
    hook = lesson["hook"].format(tickers=tickers_str)

    short = (
        f"💡 {hook}\n\n"
        f"{lesson['body'].split(chr(10))[0]}\n\n"
        f"{DISCLAIMER_SHORT}"
    )

    long = (
        f"💡 {hook}\n\n"
        f"{lesson['body']}\n\n"
        f"{lesson['cta']}\n\n"
        f"—\n"
        f"WOLF scans {len(scan.get('tickers_scanned', []))} tickers every market day. "
        f"No opinions. Just data.\n\n"
        f"{DISCLAIMER}"
    )

    return {
        "post_type": "educational",
        "topic": top_cat or "general",
        "session": session_type,
        "date": date_display,
        "platforms": {
            "twitter": short,
            "linkedin": long,
        },
        "char_count_twitter": len(short),
    }


# ─────────────────────────────────────────────
# CONTENT PACKAGE BUILDER
# ─────────────────────────────────────────────

def build_content_package(scan: Dict) -> Dict:
    """
    Build the full content package from a scan dict.
    Returns a dict with all posts + metadata.
    """
    timestamp = scan.get("timestamp", datetime.utcnow().isoformat())
    session_type = scan.get("session_type", "morning")

    try:
        dt = datetime.fromisoformat(timestamp)
        date_str = dt.strftime("%Y-%m-%d")
    except Exception:
        date_str = datetime.now().strftime("%Y-%m-%d")

    posts = []

    # Post 1: Market context (always)
    posts.append(generate_market_context_post(scan))

    # Post 2: Setup highlight (only if qualified trades exist)
    setup_post = generate_setup_highlight_post(scan)
    if setup_post:
        posts.append(setup_post)

    # Post 3: Educational (always — derived from rejections)
    posts.append(generate_educational_post(scan))

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "scan_timestamp": timestamp,
        "session_type": session_type,
        "date_str": date_str,
        "source_mode": scan.get("_source_mode", "unknown"),
        "scan_summary": {
            "tickers_scanned": len(scan.get("tickers_scanned", [])),
            "qualified_count": len(scan.get("qualified_trades", [])),
            "rejected_count": len(scan.get("rejected_trades", [])),
        },
        "posts": posts,
        "post_count": len(posts),
    }


# ─────────────────────────────────────────────
# OUTPUT WRITERS
# ─────────────────────────────────────────────

def write_json(package: Dict, output_dir: Path) -> Path:
    filename = f"bv_content_{package['date_str']}_{package['session_type'].upper()}.json"
    path = output_dir / filename
    path.write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_txt(package: Dict, output_dir: Path) -> Path:
    filename = f"bv_content_{package['date_str']}_{package['session_type'].upper()}.txt"
    path = output_dir / filename

    lines = [
        f"BEARDEDVENTURES — WOLF Content Package",
        f"Generated: {package['generated_at']}",
        f"Scan: {package['scan_timestamp']} | Session: {package['session_type'].upper()}",
        f"Tickers: {package['scan_summary']['tickers_scanned']} scanned | "
        f"{package['scan_summary']['qualified_count']} qualified | "
        f"{package['scan_summary']['rejected_count']} rejected",
        "",
        "=" * 70,
        "",
    ]

    for i, post in enumerate(package["posts"], 1):
        post_type = post["post_type"].upper().replace("_", " ")
        lines += [
            f"POST {i} — {post_type}",
            "-" * 70,
            "",
        ]

        for platform in ["twitter", "linkedin"]:
            content = post.get("platforms", {}).get(platform, "")
            if content:
                char_note = f" ({post.get('char_count_twitter', 0)} chars)" if platform == "twitter" else ""
                lines += [
                    f"[{platform.upper()}{char_note}]",
                    content,
                    "",
                ]

        lines.append("=" * 70)
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ─────────────────────────────────────────────
# DATA LOADERS
# ─────────────────────────────────────────────

def load_from_scan_log(log_path: Path, index: int = -1) -> Dict:
    """Load a scan entry from scan_log.json (lightweight mode)."""
    data = json.loads(log_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"scan_log.json is empty or not a list: {log_path}")
    entry = data[index]
    entry["_source_mode"] = "scan_log"
    return entry


def load_from_vega_file(path: Path) -> Dict:
    """Load a full VEGA payload from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = data[-1]
    data["_source_mode"] = "vega_payload"
    return data


def load_from_stdin() -> Dict:
    """Read a VEGA payload JSON from stdin."""
    raw = sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, list):
        data = data[-1]
    data["_source_mode"] = "stdin"
    return data


def load_from_jarvis(host: str) -> Dict:
    """Fetch the latest scan from JARVIS /wolf/latest endpoint."""
    if not REQUESTS_AVAILABLE:
        raise RuntimeError("requests library not available — pip install requests")
    url = f"{host.rstrip('/')}/wolf/latest"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    data["_source_mode"] = "jarvis_api"
    return data


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="BeardedVentures WOLF → Social Media Content Formatter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scan-log",
        type=Path,
        default=BASE_DIR / "logs" / "scan_log.json",
        help="Path to scan_log.json (default: logs/scan_log.json)",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=-1,
        help="Index into scan_log entries (default: -1 = latest)",
    )
    parser.add_argument(
        "--vega-file",
        type=Path,
        default=None,
        help="Path to a full VEGA payload JSON file",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read VEGA payload JSON from stdin",
    )
    parser.add_argument(
        "--from-jarvis",
        action="store_true",
        help="Fetch latest scan from JARVIS tower via /wolf/latest",
    )
    parser.add_argument(
        "--jarvis-host",
        type=str,
        default=os.environ.get("JARVIS_HOST", "http://192.168.0.222:8000"),
        help="JARVIS tower host (default: env JARVIS_HOST or 192.168.0.222:8000)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "output" / "social_content",
        help="Output directory for generated content files",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print posts to stdout only — do not write files",
    )

    args = parser.parse_args()

    # Load scan data
    try:
        if args.stdin:
            logger.info("Loading from stdin...")
            scan = load_from_stdin()
        elif args.from_jarvis:
            logger.info(f"Fetching from JARVIS at {args.jarvis_host}...")
            scan = load_from_jarvis(args.jarvis_host)
        elif args.vega_file:
            logger.info(f"Loading VEGA payload from {args.vega_file}...")
            scan = load_from_vega_file(args.vega_file)
        else:
            logger.info(f"Loading from scan log: {args.scan_log} (index {args.index})")
            scan = load_from_scan_log(args.scan_log, args.index)
    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to load scan data: {e}")
        sys.exit(1)

    logger.info(
        f"Loaded scan: {scan.get('session_type')} | "
        f"{scan.get('timestamp', 'no timestamp')} | "
        f"source: {scan.get('_source_mode')}"
    )

    # Build content
    package = build_content_package(scan)
    logger.info(f"Generated {package['post_count']} posts")

    if args.print_only:
        # Print to stdout
        for i, post in enumerate(package["posts"], 1):
            print(f"\n{'='*60}")
            print(f"POST {i} — {post['post_type'].upper()} [{post['platforms']['twitter'][:50]}...]")
            print(f"{'='*60}")
            print("\n[TWITTER/X]\n")
            print(post["platforms"]["twitter"])
            print("\n[LINKEDIN]\n")
            print(post["platforms"]["linkedin"])
        return

    # Write files
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = write_json(package, args.output_dir)
    txt_path = write_txt(package, args.output_dir)

    logger.info(f"JSON: {json_path}")
    logger.info(f"TXT:  {txt_path}")
    print(f"\n✅ Content package written:")
    print(f"   JSON: {json_path}")
    print(f"   TXT:  {txt_path}")
    print(f"\n{package['post_count']} posts generated for {package['date_str']} {package['session_type'].upper()} scan.")


if __name__ == "__main__":
    main()
