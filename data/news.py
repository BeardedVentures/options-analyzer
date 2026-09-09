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
from functools import lru_cache
from typing import Dict, List, Optional, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Keyword-based sentiment fallback
# ─────────────────────────────────────────────

# Matched on WORD BOUNDARIES against a SINGLE headline, never against the concatenated feed.
# Both of those are 2026-09-09 fixes; the old rule was `kw in " ".join(headlines).lower()`.
#
# "earnings" was removed, not reworded. EARNINGS_BLACKOUT_DAYS + the earnings_clear gate already
# measure earnings risk from an actual dated calendar, and on the same session they were right
# where this was wrong: ADBE and JPM both failed earnings_clear correctly, while SPY passed it
# correctly (an ETF has no earnings date) and was blocked here anyway, by the headline "S&P 500
# Second Quarter Earnings Outpace Forecast". A keyword is a worse proxy for a risk the system
# already measures properly, and keeping both means the worse one decides.
# Canonical term -> the surface forms that actually signal the event.
#
# Word-boundary matching is what stopped "Wall Street Fires Back" from blocking QQQ, but it also
# stopped "Adobe Recalls ... After Data Loss" from blocking ADBE: recall does not match
# "Recalls". So the forms are listed per term rather than inflected automatically.
#
# "fire" deliberately has NO plural. "Fires" in a headline is nearly always the verb -- fires
# back, fires its CEO -- while the blocking sense is the noun. Adding "fires" would re-create
# the 2026-09-09 QQQ false positive, so this asymmetry is load-bearing, not an oversight.
#
# "earnings" was removed entirely, not reworded. EARNINGS_BLACKOUT_DAYS and the earnings_clear
# gate already measure earnings risk from a dated calendar, and on the same session they were
# right where this was wrong: ADBE and JPM both failed earnings_clear correctly, while SPY
# passed it correctly (an ETF has no earnings date) and was blocked here anyway, on the headline
# "S&P 500 Second Quarter Earnings Outpace Forecast". Keeping both means the worse one decides.
BLOCKING_TERMS = {
    "fda":         ["fda"],
    # "approval" and "rejection" are NOT here. Bare, they carry no event meaning -- "approval
    # rating", "pending shareholder approval", "board approval" -- and everything they should
    # catch is already caught: regulatory decisions by "fda", deal outcomes by merger /
    # acquisition / takeover. They contributed false positives and no coverage.
    "merger":      ["merger", "mergers"],
    "acquisition": ["acquisition", "acquisitions", "acquires", "acquired"],
    "takeover":    ["takeover", "takeovers"],
    "bankruptcy":  ["bankruptcy", "bankruptcies"],
    "default":     ["default", "defaults", "defaulted"],
    "indictment":  ["indictment", "indictments", "indicted", "indicts"],
    "fraud":       ["fraud"],
    "restatement": ["restatement", "restatements", "restates", "restated"],
    "recall":      ["recall", "recalls", "recalled"],
    "hack":        ["hack", "hacks", "hacked"],
    "attack":      ["attack", "attacks", "attacked"],
    "explosion":   ["explosion", "explosions"],
    "fire":        ["fire"],
}

# Kept as a flat list for any caller that still reads it.
BLOCKING_KEYWORDS = sorted(BLOCKING_TERMS)

# Multi-word phrases, matched as substrings -- word-boundary matching on a phrase is the same
# thing, and the adjacency is what carries the meaning.
BLOCKING_PHRASES = [
    "earnings surprise", "sec charges", "data breach",
]

# A blocking headline must plausibly be ABOUT this underlying. Without this, GSK's bond sale to
# fund the Nuvalent acquisition blocked JPM -- JPM was in that headline's feed because it
# underwrites the deal -- and a single market-wrap headline blocked SPY, QQQ and IWM at once.
# Blocking the three broad-market index ETFs simultaneously on a shared signal is a category
# error: standing aside from single-name event risk is what this gate is for.
REQUIRE_TICKER_IN_HEADLINE = True

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


@lru_cache(maxsize=1)
def _alias_map():
    """{ticker: [names that stand for it in a headline]}, derived from config.WATCHLIST.

    Derived, not hand-maintained, so it cannot drift out of sync with the universe.

    Stocks get their company name: headlines say "Adobe", not "ADBE", so ticker-only matching
    would let a genuine single-name event through -- the opposite of today's failure and a worse
    one, since catching exactly that is what this gate is for.

    ETFs get the ticker ONLY. Their notes ("S&P 500", "Energy Sector ETF") are descriptions, not
    headline names, and "S&P 500" as an alias would re-create the bug: a market-wrap headline
    would block SPY again. A basket has no single-name event risk, so requiring the literal
    ticker is the correct standard for them.
    """
    out = {}
    try:
        watchlist = getattr(config, "WATCHLIST", []) or []
    except Exception:
        return out
    for row in watchlist:
        try:
            tk = str(row.get("ticker", "")).strip()
            if not tk:
                continue
            names = [tk]
            if str(row.get("type", "")).strip().lower() != "etf":
                name = str(row.get("note", "")).split(chr(8212))[0].strip()
                # Guard the derivation: a note that lost its em dash would otherwise donate a
                # whole sentence as an alias and match nearly anything.
                if name and len(name) <= 30:
                    names.append(name)
            out[tk.upper()] = names
        except Exception:
            continue
    return out


def _headline_mentions(headline, ticker, aliases=None):
    """Is this headline plausibly ABOUT this underlying?"""
    if not ticker:
        return True
    names = list(aliases) if aliases else _alias_map().get(ticker.upper(), [ticker])
    h = headline.lower()
    wb = chr(92) + "b"
    return any(re.search(wb + re.escape(n.lower()) + wb, h) for n in names)


def _blocking_hit(headline):
    """Return the canonical blocking term this ONE headline matches, or None.

    Word boundaries, not substrings. The old rule was `kw in " ".join(headlines).lower()`, which
    matched "fire" inside "Wall Street Fires Back With 'Activist Treasury'" and would equally
    match wildfire, misfired, approval rating, by default, or he recalls -- and matched it
    against the whole feed at once, so any headline could block on any other headline's word.
    On 2026-09-09 that removed QQQ from the board on a Treasury-policy headline.
    """
    h = headline.lower()
    wb = chr(92) + "b"
    for phrase in BLOCKING_PHRASES:
        if phrase in h:
            return phrase
    for term, forms in BLOCKING_TERMS.items():
        for form in forms:
            if re.search(wb + re.escape(form) + wb, h):
                return term
    return None


def _keyword_sentiment(headlines: List[str], ticker: Optional[str] = None,
                       aliases: Optional[List[str]] = None) -> Dict:
    """Rule-based sentiment. This is the PRODUCTION path, not a degraded one.

    config.DISABLE_AI is True by design -- it hard-stops paid LLM calls so paper validation
    never burns credits -- so every NEWS_BLOCK on the board comes from here. That makes this the
    one gate that disqualifies a ticker outright with no model behind it, which is exactly why
    it has to record what it matched.

    Until 2026-09-09 it recorded nothing: the rejection row said "News BLOCKING event detected"
    and carried no headline, no keyword, no source. 8 of 54 tickers were removed before any
    other gate saw them -- including SPY, QQQ and IWM -- and nothing on the artifact could be
    used to check whether a single one of them was right.
    """
    for headline in headlines:
        kw = _blocking_hit(headline)
        if not kw:
            continue
        if REQUIRE_TICKER_IN_HEADLINE and not _headline_mentions(headline, ticker, aliases):
            # Someone else's event, carried in this ticker's feed.
            logger.debug("[news] %s: '%s' in a headline that does not name it -- not blocking: %s",
                         ticker, kw, headline[:120])
            continue
        return {
            "sentiment": "BLOCKING",
            "confidence": 0.8,
            "key_themes": [kw],
            "market_impact_summary": f"Potential blocking event detected: '{kw}'",
            "blocking": True,
            # The evidence trail. A gate with veto power has to be auditable after the fact.
            "blocking_keyword": kw,
            "blocking_headline": headline[:300],
            "scoring_path": "keyword",
        }

    text = " ".join(headlines).lower()

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
            _sentiment_cache[ticker] = _keyword_sentiment(headlines, ticker=ticker)

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
    result = _keyword_sentiment(headlines, ticker=ticker)
    _sentiment_cache[ticker] = result
    return result


def get_ticker_headlines(ticker: str) -> List[str]:
    """Return cached headlines for a ticker."""
    return _headlines_cache.get(ticker, [])


def clear_cache():
    _sentiment_cache.clear()
    _headlines_cache.clear()
