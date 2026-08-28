"""Polygon entitlement failures must be visible, not silently absorbed by the yfinance fallback.

Context, 2026-08-26. Josh paid for Polygon's Options Starter plan ($29/mo) specifically to fix
thin/empty yfinance chains at the open. The key authenticates and /v3/snapshot/options returns
200 OK with real contracts (open interest, day volume) -- but `last_quote` is empty on every
contract, because quotes (NBBO bid/ask) are a separate entitlement that Starter does not include
(Developer doesn't either; only Advanced does). VEGA needs bid/ask, so every record was dropped
by `_parse_polygon_options`'s own quote check and the run fell through to yfinance -- with
nothing in the log to say why, and `validate_polygon_connection()` reporting healthy=True the
whole time because it only checked for HTTP 200 + status OK/DELAYED, never for an actual quote.

These tests pin the fix: the health check must assert a real bid/ask before calling itself
healthy, and a 200-but-no-quotes (or non-200) response must produce exactly one WARNING-level
log line per run rather than a per-ticker debug line nobody reads.
"""
import logging
from datetime import date, timedelta

import pytest

from data import fetcher


class _FakeResponse:
    def __init__(self, status_code, body, content_type="application/json"):
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": content_type}
        self.text = str(body)[:500]

    def json(self):
        return self._body


def _contract(bid=0, ask=0, mid=0, strike=100.0, exp=None, oi=500, volume=10):
    # Default expiration sits inside the 25-45 DTE window every test below queries with --
    # a fixed date would drift out of range and get filtered before the quote check ever runs.
    if exp is None:
        exp = (date.today() + timedelta(days=35)).isoformat()
    quote = {}
    if bid:
        quote["bid"] = bid
    if ask:
        quote["ask"] = ask
    if mid:
        quote["midpoint"] = mid
    return {
        "details": {"strike_price": strike, "expiration_date": exp},
        "last_quote": quote,
        "day": {"volume": volume},
        "open_interest": oi,
        "greeks": {"delta": -0.2, "theta": -0.01, "gamma": 0.01, "vega": 0.05},
        "implied_volatility": 0.30,
    }


@pytest.fixture(autouse=True)
def _reset_polygon_state(monkeypatch):
    monkeypatch.setattr(fetcher, "POLYGON_API_KEY", "test-key", raising=False)
    import config
    monkeypatch.setattr(config, "POLYGON_API_KEY", "test-key")
    fetcher.clear_cache()
    yield
    fetcher.clear_cache()


# ── validate_polygon_connection: must assert a real quote, not just HTTP 200 ──────────────────

def test_health_check_fails_when_200_ok_but_every_contract_has_no_quote(monkeypatch):
    """This is exactly what the Starter plan returned. HTTP 200, status OK, real contracts --
    and the health check must not call that healthy."""
    body = {"status": "OK", "results": [_contract(bid=0, ask=0, mid=0) for _ in range(5)]}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _FakeResponse(200, body))
    health = fetcher.validate_polygon_connection("SPY")
    assert health["healthy"] is False
    assert "quote" in health["reason"].lower()


def test_health_check_passes_when_a_real_quote_is_present(monkeypatch):
    body = {"status": "DELAYED", "results": [
        _contract(bid=0, ask=0, mid=0),
        _contract(bid=1.20, ask=1.35, mid=1.28),
    ]}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _FakeResponse(200, body))
    health = fetcher.validate_polygon_connection("SPY")
    assert health["healthy"] is True


def test_health_check_still_fails_on_non_200(monkeypatch):
    monkeypatch.setattr(
        fetcher.requests, "get",
        lambda *a, **k: _FakeResponse(403, {"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}),
    )
    health = fetcher.validate_polygon_connection("SPY")
    assert health["healthy"] is False
    assert "403" in health["reason"]


def test_health_check_does_not_crash_on_zero_results(monkeypatch):
    body = {"status": "OK", "results": []}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _FakeResponse(200, body))
    health = fetcher.validate_polygon_connection("SPY")
    assert health["healthy"] is True   # not an entitlement failure, just an empty window


# ── _parse_polygon_options: the entitlement gap must warn once, not vanish into debug ─────────

def test_all_contracts_missing_quotes_logs_one_warning(monkeypatch, caplog):
    body = {"status": "OK", "results": [_contract(bid=0, ask=0, mid=0) for _ in range(8)],
            "next_url": None}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _FakeResponse(200, body))
    with caplog.at_level(logging.WARNING, logger="data.fetcher"):
        records = fetcher._parse_polygon_options("SPY", 500.0, min_dte=25, max_dte=45)
    assert records == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
    assert "not entitled" in warnings[0].message.lower() or "no bid/ask" in warnings[0].message.lower()


def test_the_warning_fires_once_per_run_not_once_per_ticker(monkeypatch, caplog):
    body = {"status": "OK", "results": [_contract(bid=0, ask=0, mid=0) for _ in range(3)],
            "next_url": None}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _FakeResponse(200, body))
    with caplog.at_level(logging.WARNING, logger="data.fetcher"):
        fetcher._parse_polygon_options("SPY", 500.0, min_dte=25, max_dte=45)
        fetcher._parse_polygon_options("AAPL", 200.0, min_dte=25, max_dte=45)
        fetcher._parse_polygon_options("QQQ", 400.0, min_dte=25, max_dte=45)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "56 tickers hitting the same entitlement gap must not produce 56 log lines"


def test_clear_cache_resets_the_once_per_run_warning(monkeypatch, caplog):
    body = {"status": "OK", "results": [_contract(bid=0, ask=0, mid=0)], "next_url": None}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _FakeResponse(200, body))
    with caplog.at_level(logging.WARNING, logger="data.fetcher"):
        fetcher._parse_polygon_options("SPY", 500.0, min_dte=25, max_dte=45)
        fetcher.clear_cache()   # start of next scan
        fetcher._parse_polygon_options("SPY", 500.0, min_dte=25, max_dte=45)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2, "a new scan must be able to warn again, not stay silenced forever"


def test_a_real_quote_produces_no_warning_and_a_real_record(monkeypatch, caplog):
    body = {"status": "OK", "results": [_contract(bid=1.20, ask=1.35, mid=1.28)], "next_url": None}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: _FakeResponse(200, body))
    with caplog.at_level(logging.WARNING, logger="data.fetcher"):
        records = fetcher._parse_polygon_options("SPY", 500.0, min_dte=25, max_dte=45)
    assert len(records) == 1
    assert records[0]["bid"] == 1.20 and records[0]["ask"] == 1.35
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_non_200_logs_one_warning_with_the_entitlement_hint(monkeypatch, caplog):
    monkeypatch.setattr(
        fetcher.requests, "get",
        lambda *a, **k: _FakeResponse(403, {"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}),
    )
    with caplog.at_level(logging.WARNING, logger="data.fetcher"):
        records = fetcher._parse_polygon_options("SPY", 500.0, min_dte=25, max_dte=45)
    assert records == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "403" in warnings[0].message
