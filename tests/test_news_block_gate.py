"""The news gate must show its work, and match words rather than substrings (2026-09-09).

NEWS_BLOCK is the only gate that disqualifies a ticker outright with no model behind it --
config.DISABLE_AI is True by design, so every NEWS_BLOCK on the board comes from the rule-based
scorer. On 2026-09-09 it removed 8 of 54 tickers, including SPY, QQQ and IWM, and the rejection
rows carried no headline, no keyword and no source, so none of it could be checked. Checked by
hand, most of it was wrong. Three distinct defects produced that:

  1. `kw in " ".join(headlines).lower()` -- substring, against the CONCATENATED feed. "fire"
     matched inside "Wall Street Fires Back With 'Activist Treasury'", and any headline could
     block on any other headline's word.
  2. No requirement that the blocking headline be ABOUT the ticker. GSK's bond sale to fund the
     Nuvalent acquisition blocked JPM (JPM underwrites the deal, so it rode along in the feed),
     and one market-wrap headline took out SPY, QQQ and IWM together.
  3. "earnings" as a keyword, duplicating EARNINGS_BLACKOUT_DAYS + the earnings_clear gate,
     which measure the same risk from a dated calendar -- and disagreeing with them: SPY passed
     earnings_clear correctly (an ETF has no earnings date) and was blocked here anyway.

Every case below is drawn from that session's real headlines or is a direct adversarial probe of
the fix. The false-positive cases all pass under the old substring rule and fail under it now.
"""
import pytest

from data import news


# -- The 2026-09-09 false positives: real headlines that must NOT block ------------------------

@pytest.mark.parametrize("ticker, headline, defect", [
    ("QQQ",  "Wall Street Fires Back With 'Activist Treasury'",           "substring 'fire' in 'Fires'"),
    ("JPM",  "GSK Commences Bond Sale to Help Fund Nuvalent Acquisition",  "another company's event"),
    ("SPY",  "S&P 500 Second Quarter Earnings Outpace Forecast",           "market wrap + 'earnings'"),
    ("IWM",  "S&P 500 Second Quarter Earnings Outpace Forecast",           "market wrap + 'earnings'"),
    ("ADBE", "Adobe's Fiscal Q3 Report Likely to Show Faster User Growth", "earnings_clear owns this"),
])
def test_real_headlines_that_wrongly_blocked(ticker, headline, defect):
    result = news._keyword_sentiment([headline], ticker=ticker)
    assert not result.get("blocking"), f"{ticker} still blocked on {defect}: {headline}"


def test_substring_would_have_matched():
    """Pins the mechanism, so a revert to `kw in text` fails here and not just above."""
    headline = "Wall Street Fires Back With 'Activist Treasury'"
    assert "fire" in headline.lower(), "premise: the old substring rule did match"
    assert news._blocking_hit(headline) is None, "word-boundary matching must not"


def test_one_headline_cannot_block_on_another_headlines_word():
    """The old rule scored the concatenated feed, so any headline poisoned the whole batch."""
    feed = [
        "Chevron Explosion at Refinery Kills Two",   # blocking, and not about NKE
        "Nike Names New Chief Marketing Officer",    # benign
    ]
    assert " ".join(feed).lower().count("explosion") == 1, "premise: the word is in the feed"
    assert not news._keyword_sentiment(feed, ticker="NKE").get("blocking")


def test_broad_market_etfs_do_not_block_together_on_a_shared_headline():
    """Blocking SPY, QQQ and IWM at once is a category error: a basket has no single-name event."""
    headline = "S&P 500 Second Quarter Earnings Outpace Forecast as Reporting Season Nears End"
    blocked = [t for t in ("SPY", "QQQ", "IWM")
               if news._keyword_sentiment([headline], ticker=t).get("blocking")]
    assert blocked == [], f"market wrap still blocks {blocked}"


# -- Genuine single-name events must STILL block -----------------------------------------------
#
# A gate that blocks nothing is as broken as one that blocks everything; the fix is only correct
# if it is narrower, not weaker.

@pytest.mark.parametrize("ticker, headline, term", [
    ("PFE",  "FDA Rejects Pfizer Arthritis Drug Application",          "fda"),
    ("PFE",  "FDA Approves Pfizer Vaccine for Children",               "fda"),
    ("BA",   "Boeing 737 Explosion Halts Production Line",             "explosion"),
    ("CRWD", "CrowdStrike Discloses Data Breach Affecting Customers",  "data breach"),
    ("JPM",  "JPMorgan Faces SEC Charges Over Trading Desk",           "sec charges"),
    ("NKE",  "Nike Announces Acquisition of Rival Brand",              "acquisition"),
    ("COIN", "Coinbase Hit by Hack, Funds Moved",                      "hack"),
    ("GE",   "General Electric Indicted in Procurement Probe",         "indictment"),
    ("SPY",  "SPY Options Halted After Exchange Hack",                 "hack"),
])
def test_genuine_events_still_block(ticker, headline, term):
    result = news._keyword_sentiment([headline], ticker=ticker)
    assert result.get("blocking"), f"{ticker} no longer blocks on {term}: {headline}"
    assert result.get("blocking_keyword") == term


@pytest.mark.parametrize("ticker, headline", [
    ("ADBE", "Adobe Recalls Creative Cloud Update After Data Loss"),
    ("TSLA", "Tesla Recalled 12,000 Vehicles Over Brake Fault"),
])
def test_word_boundaries_still_catch_inflections(ticker, headline):
    r"""Word boundaries broke plurals -- \brecall\b misses "Recalls" -- so forms are explicit."""
    assert news._keyword_sentiment([headline], ticker=ticker).get("blocking")


def test_fire_has_no_plural_form():
    """Load-bearing asymmetry: "Fires" is the verb, and adding it re-creates the QQQ bug."""
    assert "fires" not in news.BLOCKING_TERMS["fire"]


# -- Terms that were removed, and why ----------------------------------------------------------

def test_earnings_is_not_a_blocking_keyword():
    """A dated calendar gate already measures this, and disagreed with the keyword."""
    assert "earnings" not in news.BLOCKING_TERMS
    assert "earnings" not in news.BLOCKING_KEYWORDS


@pytest.mark.parametrize("term", ["approval", "rejection"])
def test_bare_approval_and_rejection_are_not_terms(term):
    """Bare, they carry no event meaning; fda / merger / acquisition cover the real cases."""
    assert term not in news.BLOCKING_TERMS


def test_approval_rating_does_not_block():
    assert not news._keyword_sentiment(
        ["Analysts See Nike Approval Rating Improving Among Teens"], ticker="NKE").get("blocking")


# -- The evidence trail ------------------------------------------------------------------------

def test_block_records_what_it_matched():
    """Without this the 8-of-54 block list could not be audited by anyone, including its author."""
    feed = [
        "Stocks Rally as Investors Weigh Rate Path",
        "FDA Rejects Pfizer Arthritis Drug Application",
        "Pfizer Names New CFO",
    ]
    result = news._keyword_sentiment(feed, ticker="PFE")
    assert result["blocking"] is True
    assert result["blocking_keyword"] == "fda"
    # The specific headline, not the whole feed.
    assert result["blocking_headline"] == "FDA Rejects Pfizer Arthritis Drug Application"
    assert result["scoring_path"] == "keyword"


def test_non_blocking_result_carries_no_stale_evidence():
    result = news._keyword_sentiment(["Nike Names New Chief Marketing Officer"], ticker="NKE")
    assert not result.get("blocking")
    assert not result.get("blocking_keyword")


# -- The alias map, which is what makes "about this ticker" decidable --------------------------

def test_stocks_get_their_company_name():
    """Headlines say "Adobe", not "ADBE"; ticker-only matching would miss real events."""
    assert "Adobe" in news._alias_map()["ADBE"]
    assert "JPMorgan" in news._alias_map()["JPM"]


def test_etfs_get_the_ticker_only():
    """An "S&P 500" alias would re-create the market-wrap false positive."""
    assert news._alias_map()["SPY"] == ["SPY"]
    assert news._alias_map()["XLE"] == ["XLE"]


def test_alias_map_covers_the_whole_watchlist():
    """Derived from config.WATCHLIST so it cannot drift out of sync with the universe."""
    import config
    tickers = {r["ticker"].upper() for r in config.WATCHLIST}
    assert tickers <= set(news._alias_map())


# -- Plumbing: the gate is a no-op unless callers pass the ticker ------------------------------

def test_callers_pass_the_ticker():
    """REQUIRE_TICKER_IN_HEADLINE does nothing when ticker is None, and both call sites default
    it. This asserts the wiring, which is where the first version of the fix was silently dead."""
    import inspect
    src = inspect.getsource(news)
    calls = [ln.strip() for ln in src.splitlines()
             if "_keyword_sentiment(headlines" in ln and "def " not in ln]
    assert calls, "premise: the internal call sites exist"
    for call in calls:
        assert "ticker=" in call, f"call site drops the ticker: {call}"
