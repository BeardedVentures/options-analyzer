"""
data/news.py — News retrieval and sentiment scoring.

Two-tier system:
  Tier 1: Headline fetch (NewsAPI → yfinance fallback, free)
  Tier 2: Sentiment scoring (GPT-4o batch → keyword fallback)

All tickers batched into a single GPT-4o call to minimize cost.
"""

import json
import logging
import re
from typing import Dict, List, Optional, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Keyword-based sentiment fallback
# ─────────────────────────────────────────────

BLOCKING_KEYWORDS = [
    "earnings", "earnings surprise", "fda", "approval", "rejection",
    "merger", "acquisition", "takeover", "bankruptcy", "default",
    "indictment", "sec charges", "fraud", "restatement", "recall",
    "data breach", "hack", "attack", "explosion", "fire",
]

NEGATIVE_KEYWORDS = [
    "downgrade", "miss", "below expectations", "warning", "loss",
    "decline", "fall", "drop", "crash", "sell-off", "recession",
    "layoffs", "cut", "reduce", "concern", "risk", "fear",
    "inflation", "rate hike", "tightening",
]

POSITIVE_KEYWORDS = [
    "upgrade", "beat", "above expectations", "record", "growth",
    "profit", "revenue", "buyback", "dividend", "raise", "expand",
    "partnership", "deal", "rally", "surge", "breakthrough",
]


def _keyword_sentiment(headlines: List[str]) -> Dict:
    """
    Simple keyword-based sentiment scoring.
    Used when OpenAI API key is not configured.
    """
    text = " ".join(headlines).lower()

    for kw in BLOCKING_KEYWORDS:
        if kw in text:
            return {
                "sentiment": "BLOCKING",
                "confidence": 0.8,
                "key_themes": [kw],
                "market_impact_summary": f"Potential blocking event detected: '{kw}'",
                "blocking": True,
            }

    neg_count = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
    pos_count = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)

    if neg_count > pos_count + 1:
        return {
            "sentiment": "NEGATIVE",
            "confidence": 0.6,
            "key_themes": [kw for kw in NEGATIVE_KEYWORDS if kw in text][:3],
            "market_impact_summary": "Negative news themes detected — monitor closely.",
            "blocking": False,
        }
    elif pos_count > neg_count:
        return {
            "sentiment": "POSITIVE",
            "confidence": 0.6,
            "key_themes": [kw for kw in POSITIVE_KEYWORDS if kw in text][:3],
            "market_impact_summary": "Positive news themes — favorable for premium selling.",
            "blocking": False,
        }
    else:
        return {
            "sentiment": "NEUTRAL",
            "confidence": 0.5,
            "key_themes": [],
            "market_impact_summary": "No significant news impact detected.",
            "blocking": False,
        }


# ─────────────────────────────────────────────
# GPT-4o batch sentiment scoring
# ─────────────────────────────────────────────

def _gpt4o_batch_sentiment(ticker_headlines: Dict[str, List[str]]) -> Dict[str, Dict]:
    """
    Batch all ticker headlines into a single GPT-4o call.
    Returns {ticker: sentiment_dict} for each ticker.
    """
    if not config.OPENAI_API_KEY:
        return {}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)

        input_data = {
            ticker: headlines[:8]  # cap per ticker
            for ticker, headlines in ticker_headlines.items()
            if headlines
        }

        if not input_data:
            return {}

        system_prompt = (
            "You are a financial news analyst. Score each ticker's news headlines for their "
            "likely impact on SHORT-TERM options premium SELLING (bull put spreads, iron condors). "
            "Return ONLY valid JSON. No markdown, no explanation.\n\n"
            "For each ticker, return:\n"
            '  "sentiment": "POSITIVE" | "NEUTRAL" | "NEGATIVE" | "BLOCKING"\n'
            '  "confidence": float 0-1\n'
            '  "key_themes": list of strings\n'
            '  "market_impact_summary": one sentence\n\n'
            "BLOCKING = earnings surprise, FDA decision, merger announcement, "
            "legal action, data breach — anything that creates unpredictable gap risk. "
            "NEGATIVE = bad for premium sellers (volatility spike risk, downside risk). "
            "POSITIVE = stable/bullish environment — favorable for selling premium. "
            "NEUTRAL = no significant impact."
        )

        user_content = (
            "Score the following tickers' news:\n\n"
            + json.dumps(input_data, indent=2)
            + "\n\nReturn JSON: {\"TICKER\": {sentiment, confidence, key_themes, market_impact_summary}, ...}"
        )

        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=1200,
            temperature=0.1,
        )

        raw = response.choices[0].message.content.strip()

        # Extract JSON from response
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            # Normalize
            result = {}
            for ticker, vals in data.items():
                result[ticker] = {
                    "sentiment": vals.get("sentiment", "NEUTRAL"),
                    "confidence": float(vals.get("confidence", 0.5)),
                    "key_themes": vals.get("key_themes", []),
                    "market_impact_summary": vals.get("market_impact_summary", ""),
                    "blocking": vals.get("sentiment") == "BLOCKING",
                }
            logger.info(f"[news] GPT-4o scored {len(result)} tickers in one call")
            return result

    except Exception as e:
        logger.warning(f"[news] GPT-4o batch sentiment error: {e}")

    return {}


# ─────────────────────────────────────────────
# Session-level cache and main interface
# ─────────────────────────────────────────────

_sentiment_cache: Dict[str, Dict] = {}
_headlines_cache: Dict[str, List[str]] = {}


def analyze_all_tickers(tickers: List[str]) -> Dict[str, Dict]:
    """
    Fetch headlines and score sentiment for all tickers in one pass.
    Caches results. Call once per session, then use get_ticker_sentiment().

    Returns {ticker: sentiment_dict}
    """
    from data import fetcher

    # Fetch headlines for all tickers
    ticker_headlines: Dict[str, List[str]] = {}
    for ticker in tickers:
        articles = fetcher.get_news(ticker)
        headlines = [a["title"] for a in articles if a.get("title")]
        _headlines_cache[ticker] = headlines
        ticker_headlines[ticker] = headlines

    # Try GPT-4o batch first
    gpt_results = _gpt4o_batch_sentiment(ticker_headlines)

    # Fill in any missing with keyword fallback
    for ticker in tickers:
        if ticker in gpt_results:
            _sentiment_cache[ticker] = gpt_results[ticker]
        else:
            headlines = ticker_headlines.get(ticker, [])
            _sentiment_cache[ticker] = _keyword_sentiment(headlines)

    logger.info(f"[news] Sentiment analysis complete for {len(tickers)} tickers")
    return _sentiment_cache


def get_ticker_sentiment(ticker: str) -> Dict:
    """
    Return sentiment dict for a single ticker.
    Falls back to keyword analysis if not yet cached.
    """
    if ticker in _sentiment_cache:
        return _sentiment_cache[ticker]

    # Not yet analyzed — run keyword fallback on cached headlines
    headlines = _headlines_cache.get(ticker, [])
    result = _keyword_sentiment(headlines)
    _sentiment_cache[ticker] = result
    return result


def get_ticker_headlines(ticker: str) -> List[str]:
    """Return cached headlines for a ticker."""
    return _headlines_cache.get(ticker, [])


def clear_cache():
    _sentiment_cache.clear()
    _headlines_cache.clear()
