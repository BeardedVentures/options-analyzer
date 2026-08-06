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
    if getattr(config, "DISABLE_AI", False) or not config.OPENAI_API_KEY:
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
        err_str = str(e).lower()
        # Detect model deprecation specifically — this is the signal to update config.OPENAI_MODEL
        if "model_not_found" in err_str or "model" in err_str and "deprecated" in err_str:
            logger.critical(
                f"[news] OPENAI MODEL DEPRECATED — update config.OPENAI_MODEL. "
                f"Current value: '{config.OPENAI_MODEL}'. Falling back to keyword sentiment. Error: {e}"
            )
        elif "model_not_found" not in err_str:
            # Try fallback model before giving up
            try:
                fallback_model = getattr(config, "OPENAI_MODEL_FALLBACK", "gpt-4o-mini")
                logger.warning(
                    f"[news] Primary model '{config.OPENAI_MODEL}' failed ({e}). "
                    f"Trying fallback '{fallback_model}'."
                )
                response = client.chat.completions.create(
                    model=fallback_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=1200,
                    temperature=0.1,
                )
                raw = response.choices[0].message.content.strip()
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    result = {}
                    for ticker, vals in data.items():
                        result[ticker] = {
                            "sentiment": vals.get("sentiment", "NEUTRAL"),
                            "confidence": float(vals.get("confidence", 0.5)),
                            "key_themes": vals.get("key_themes", []),
                            "market_impact_summary": vals.get("market_impact_summary", ""),
                            "blocking": vals.get("sentiment") == "BLOCKING",
                        }
                    logger.info(f"[news] Fallback model scored {len(result)} tickers")
                    return result
            except Exception as fallback_e:
                logger.warning(f"[news] Fallback model also failed: {fallback_e}")
        else:
            logger.warning(f"[news] GPT batch sentiment error: {e}")

    return {}


# ─────────────────────────────────────────────
# Session-level cache and main interface
# ─────────────────────────────────────────────

_sentiment_cache: Dict[str, Dict] = {}
_headlines_cache: Dict[str, List[str]] = {}

# Persistent (cross-process) sentiment cache. The cockpit re-scans the board every ~15 min but
# only re-scrapes news ~hourly; the 15-min scans in between load this file instead of hitting the
# headline APIs (and the GPT-4o sentiment call) again. Written by whichever run does a fresh scrape.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
_DISK_CACHE_PATH = os.path.join(_CACHE_DIR, "sentiment_cache.json")


def _cache_age_minutes(updated_at: str) -> Optional[float]:
    """Age of an ISO-8601 timestamp in minutes, or None if unparseable."""
    from datetime import datetime
    try:
        updated = datetime.fromisoformat(updated_at)
        return (datetime.now(updated.tzinfo) - updated).total_seconds() / 60.0
    except Exception:
        return None


def _load_disk_cache(tickers: List[str], max_age_min: float) -> Optional[Dict[str, Dict]]:
    """Return the on-disk sentiment map if it's fresh enough and covers every requested ticker."""
    try:
        with open(_DISK_CACHE_PATH, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return None
    age = _cache_age_minutes(blob.get("updated_at", ""))
    if age is None or age > max_age_min:
        return None
    sentiment = blob.get("sentiment") or {}
    if not all(t in sentiment for t in tickers):
        return None  # watchlist changed — force a fresh scrape
    logger.info(f"[news] Using disk sentiment cache (age {age:.0f} min ≤ {max_age_min:.0f} min TTL)")
    _sentiment_cache.update(sentiment)
    _headlines_cache.update(blob.get("headlines") or {})
    return dict(sentiment)


def _save_disk_cache(sentiment: Dict[str, Dict], headlines: Dict[str, List[str]]) -> None:
    from datetime import datetime
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        payload = {
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sentiment": sentiment,
            "headlines": headlines,
        }
        with open(_DISK_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except Exception as exc:  # cache write must never break a scan
        logger.warning(f"[news] Could not write sentiment cache: {exc}")


def analyze_all_tickers(
    tickers: List[str],
    force_refresh: bool = False,
    max_age_min: Optional[float] = None,
) -> Dict[str, Dict]:
    """
    Fetch headlines and score sentiment for all tickers in one pass.

    Uses a persistent disk cache (data/cache/sentiment_cache.json): if that cache is younger than
    the TTL (config.NEWS_CACHE_TTL_MIN, override via max_age_min) and covers every requested ticker,
    it is returned without re-scraping — this is what lets the cockpit's 15-min board refreshes stay
    cheap while news genuinely re-scrapes only ~hourly. Pass force_refresh=True (the hourly path) to
    always scrape and rewrite the cache.

    Returns {ticker: sentiment_dict}
    """
    if max_age_min is None:
        max_age_min = float(getattr(config, "NEWS_CACHE_TTL_MIN", 60))

    if not force_refresh:
        cached = _load_disk_cache(tickers, max_age_min)
        if cached is not None:
            return cached

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

    _save_disk_cache(
        {t: _sentiment_cache[t] for t in tickers if t in _sentiment_cache},
        {t: _headlines_cache.get(t, []) for t in tickers},
    )
    logger.info(f"[news] Sentiment analysis complete for {len(tickers)} tickers (fresh scrape)")
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
