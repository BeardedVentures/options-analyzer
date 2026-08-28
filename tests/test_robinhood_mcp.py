"""Robinhood Agentic Trading MCP — the guards that keep it out of a headless cycle.

The server itself is real. Verified 2026-08-27 by direct probe:

    POST https://agent.robinhood.com/mcp/trading
      -> 401, WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/…"
    GET  /.well-known/oauth-authorization-server
      -> 200  PKCE S256, grant_types ["authorization_code", "refresh_token"],
              token endpoint https://api.robinhood.com/oauth2/token/

What was NOT verified is the shape of a `get_option_quotes` response — the parser in
fetcher._parse_robinhood_options was written from published tool *names*, and the integration
placed it at TIER 1 of the chain used by an unattended hourly cycle, enabled by default.

Two hazards follow, and every test here pins one of them.

1. Interactive OAuth inside a scheduled run. The cycle launches hidden and non-interactive.
   Reaching the browser-approval path there opens a tab nobody sees and then blocks on the
   callback wait — originally 300 s, PER TICKER, against a 30-minute task timeout on a cycle
   that already runs 11–16 minutes. That is the 2026-08-25 close-scan death rebuilt from new
   parts, and it would have armed itself the moment `pip install -r requirements.txt` pulled
   in the mcp SDK.

2. Paying a known-dead path 56 times. A source that cannot serve the first ticker will not
   serve the 55 after it.
"""
import sys

import pytest

import config
from data import fetcher
from data import robinhood_mcp


# ── The engine must not adopt an unproven source on its own ──────────────────────────────

def test_the_code_default_stays_off_even_though_env_enables_it():
    """Defence in depth survives the rollout.

    This asserted `config.ROBINHOOD_MCP_ENABLED is False` while the parser was unverified. It
    was verified against a live response and deliberately enabled in .env on 2026-08-27, so
    that assertion now encodes a stale expectation rather than a property worth keeping.

    What still matters is the fallback: every read of the flag defaults to OFF when it is
    absent. A config that fails to load, a stripped .env, a fresh checkout -- none of them may
    silently turn a brokerage connection on. So test the DEFAULT, not the current value.
    """
    import data.fetcher as f
    import inspect
    src = inspect.getsource(f._parse_robinhood_options)
    assert 'getattr(config, "ROBINHOOD_MCP_ENABLED", False)' in src, (
        "the parser's flag read must default to False when config is missing the attribute")


def test_browser_auth_is_off_by_default():
    assert config.ROBINHOOD_MCP_ALLOW_BROWSER is False


def test_the_parser_refuses_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", False, raising=False)
    assert fetcher._parse_robinhood_options("SPY", 640.0, 25, 45) == []


# ── Hazard 1: no browser in a non-interactive process ────────────────────────────────────

def _run(coro):
    """_redirect_handler is a coroutine; there is no async plugin installed, so drive it here."""
    import asyncio
    return asyncio.run(coro)


def test_no_browser_opens_when_the_flag_is_unset(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ALLOW_BROWSER", False, raising=False)
    opened = []
    monkeypatch.setattr(robinhood_mcp.webbrowser, "open", lambda u: opened.append(u))
    with pytest.raises(robinhood_mcp.InteractiveAuthRequired):
        _run(robinhood_mcp._redirect_handler("https://robinhood.com/oauth?x=1"))
    assert opened == [], "a scheduled run must never open a browser tab"


def test_no_browser_opens_without_a_tty_even_if_the_flag_is_set(monkeypatch):
    """The flag alone is too weak — a profile that sets it would be inherited by the task."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ALLOW_BROWSER", True, raising=False)

    class _NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _NoTTY())
    opened = []
    monkeypatch.setattr(robinhood_mcp.webbrowser, "open", lambda u: opened.append(u))
    with pytest.raises(robinhood_mcp.InteractiveAuthRequired):
        _run(robinhood_mcp._redirect_handler("https://robinhood.com/oauth?x=1"))
    assert opened == []


def test_the_browser_does_open_when_a_human_is_present(monkeypatch):
    """The guard must not be so tight that the authorization step itself cannot run."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ALLOW_BROWSER", True, raising=False)

    class _TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _TTY())
    opened = []
    monkeypatch.setattr(robinhood_mcp.webbrowser, "open", lambda u: opened.append(u))
    _run(robinhood_mcp._redirect_handler("https://robinhood.com/oauth?x=1"))
    assert opened == ["https://robinhood.com/oauth?x=1"]


def test_the_callback_wait_is_bounded_by_config():
    """300 s was long enough to blow the cycle timeout on its own."""
    assert config.ROBINHOOD_MCP_CALLBACK_TIMEOUT <= 180


# ── Hazard 2: don't pay a dead path once per ticker ──────────────────────────────────────

def _enable(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    # BOTH pieces of run state. Clearing only the latch left the failure counter carrying over
    # between tests, so a test that ran after a few simulated failures started partway to the
    # threshold and passed or failed depending on suite ORDER -- green alone, red in the suite.
    fetcher._robinhood_unavailable_this_run.clear()
    fetcher._robinhood_failures.clear()


def test_a_raising_source_is_tried_once_then_skipped(monkeypatch):
    _enable(monkeypatch)
    calls = []

    def boom(ticker, url, option_type='put', **kw):
        calls.append(ticker)
        raise RuntimeError("needs browser approval")

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", boom)
    for tk in ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]:
        assert fetcher._parse_robinhood_options(tk, 100.0, 25, 45) == []
    assert calls == ["SPY"], f"path was retried per ticker: {calls}"


def test_a_silent_none_counts_toward_the_latch_but_does_not_trip_it_alone(monkeypatch):
    """fetch_put_chain swallows its own errors and returns None, so None is the common shape of
    an auth wall — but ALSO of an ordinary empty answer.

    This originally asserted that one None latched the source immediately. That is exactly what
    broke the first live cycle on 2026-08-27: SPY was served correctly, a second SPY call for a
    different DTE window came back empty, and 56 tickers went to yfinance. Counting toward a
    threshold keeps the protection against a genuinely dead source without letting one odd
    query shape speak for the whole run.
    """
    _enable(monkeypatch)
    calls = []

    def nothing(ticker, url, option_type='put', **kw):
        calls.append(ticker)
        return None

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", nothing)
    for tk in ["SPY", "QQQ", "IWM"]:
        assert fetcher._parse_robinhood_options(tk, 100.0, 25, 45) == []
    assert calls == ["SPY", "QQQ", "IWM"], "one empty answer must not end the run"
    assert not fetcher._robinhood_unavailable_this_run, "3 is under the threshold"


def test_the_latch_resets_between_scans(monkeypatch):
    """A transient outage must not disable the source until the process restarts."""
    _enable(monkeypatch)
    calls = []
    monkeypatch.setattr(robinhood_mcp, "fetch_chain",
                        lambda t, u, ot='put', **kw: calls.append(t) or None)
    limit = fetcher._ROBINHOOD_FAILURE_LIMIT
    for i in range(limit + 3):                  # drive it past the threshold
        fetcher._parse_robinhood_options(f"T{i}", 100.0, 25, 45)
    assert len(calls) == limit, "should stop trying once the threshold is reached"
    fetcher.clear_cache()                        # a new scan gets a clean slate
    fetcher._parse_robinhood_options("AFTER", 100.0, 25, 45)
    assert calls[-1] == "AFTER", "a transient outage must not disable the source until restart"


def test_a_missing_optional_dependency_degrades_the_source_not_the_scan(monkeypatch):
    """The mcp SDK is optional. Its absence must cost this source and nothing else."""
    _enable(monkeypatch)

    def import_error(ticker, url, option_type='put', **kw):
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", import_error)
    assert fetcher._parse_robinhood_options("SPY", 100.0, 25, 45) == []


# ── The health check must be able to fail ────────────────────────────────────────────────

def test_health_reports_disabled_rather_than_healthy_when_switched_off(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", False, raising=False)
    h = fetcher.validate_robinhood_connection("SPY")
    assert h["mode"] == "disabled" and h["enabled"] is False


def test_health_fails_when_the_response_carries_no_quote(monkeypatch):
    """The Polygon lesson: a call that succeeds is not a call that returned a usable price."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    monkeypatch.setattr(robinhood_mcp, "fetch_chain",
                        lambda s, u, ot="put", **kw: {"quotes": [{"strike": 100, "open_interest": 5}]})
    h = fetcher.validate_robinhood_connection("SPY")
    assert h["healthy"] is False


def test_health_passes_on_a_real_quote(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    monkeypatch.setattr(robinhood_mcp, "fetch_chain",
                        lambda s, u, ot="put", **kw: {"quotes": [{"strike": 100, "bid": 1.2, "ask": 1.3}]})
    h = fetcher.validate_robinhood_connection("SPY")
    assert h["healthy"] is True


def test_health_does_not_raise_when_the_sdk_is_absent(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)

    def import_error(s, u, option_type='put', **kw):
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", import_error)
    h = fetcher.validate_robinhood_connection("SPY")
    assert h["healthy"] is False and "mcp" in h["reason"]


# ── Read-only enforcement ────────────────────────────────────────────────────────────────
#
# This talks to Robinhood's Agentic TRADING server against a real brokerage account, and the
# OAuth grant is one broad scope ("internal") -- there is no read-only scope to request, so the
# minted token would very likely be accepted for order placement. The operator's requirement is
# unambiguous: VEGA is a data source, and a human places every order in Robinhood's own
# interface.
#
# "The current call sites happen to be reads" is not that guarantee. These tests make it one.

class _FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append(name)
        return {"called": name}


@pytest.mark.parametrize("tool", [
    "place_option_order", "place_order", "submit_order", "cancel_order",
    "buy_option", "sell_option", "modify_order", "execute_trade",
])
def test_order_tools_are_blocked(tool):
    sess = _FakeSession()
    with pytest.raises(robinhood_mcp.WriteToolBlocked):
        _run(robinhood_mcp._call_read_tool(sess, tool, {}))
    assert sess.calls == [], f"{tool} reached the server"


def test_an_unknown_tool_is_blocked_by_default():
    """Deny-by-default: a tool nobody anticipated must not get through just because no one
    thought to name it."""
    sess = _FakeSession()
    with pytest.raises(robinhood_mcp.WriteToolBlocked):
        _run(robinhood_mcp._call_read_tool(sess, "some_tool_added_next_year", {}))
    assert sess.calls == []


@pytest.mark.parametrize("tool", sorted(robinhood_mcp.READ_ONLY_TOOLS))
def test_allowlisted_reads_pass(tool):
    """The guard must not be so tight that the data source cannot function."""
    sess = _FakeSession()
    _run(robinhood_mcp._call_read_tool(sess, tool, {}))
    assert sess.calls == [tool]


def test_the_allowlist_contains_no_write_verbs():
    forbidden = ("place", "order", "buy", "sell", "cancel", "submit", "execute", "modify")
    for tool in robinhood_mcp.READ_ONLY_TOOLS:
        assert not any(v in tool.lower() for v in forbidden), f"{tool} looks like a write tool"


def test_no_call_site_bypasses_the_guard():
    """Every path from this module to the server must go through _call_read_tool. A raw
    session.call_tool() elsewhere would silently reopen the hole."""
    import inspect
    src = inspect.getsource(robinhood_mcp)
    raw = [ln.strip() for ln in src.splitlines()
           if "session.call_tool(" in ln and "_call_read_tool" not in ln]
    # The single legitimate occurrence is the one inside _call_read_tool itself.
    assert len(raw) == 1 and raw[0].startswith("return await session.call_tool"), (
        f"unguarded call sites found: {raw}")


# ── CancelledError is not an Exception ───────────────────────────────────────────────────
#
# The MCP client runs its transport inside an anyio task group, so a failure raised in there --
# including the InteractiveAuthRequired this module raises deliberately -- comes back out as
# asyncio.CancelledError. That derives from BaseException, NOT Exception, so `except Exception`
# silently did not apply. Verified live on 2026-08-27: an auth refusal escaped fetch_put_chain,
# then escaped fetcher._parse_robinhood_options, and would have reached the scan.
#
# This is the failure mode the whole "degrade gracefully, never crash" contract exists to stop,
# and it was invisible to every test that used a plain Exception as its stand-in.

import asyncio


def test_fetch_put_chain_contains_a_cancellederror(monkeypatch):
    """Its docstring promises it never raises. CancelledError was the exception to that."""
    def cancelled(*a, **k):
        raise asyncio.CancelledError("Cancelled via cancel scope 0xdeadbeef")

    monkeypatch.setattr(robinhood_mcp.asyncio, "run", cancelled)
    assert robinhood_mcp.fetch_put_chain("SPY", "https://example.invalid") is None


def test_the_parser_contains_a_cancellederror(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    fetcher._robinhood_unavailable_this_run.clear()

    def cancelled(ticker, url, option_type='put', **kw):
        raise asyncio.CancelledError("Cancelled via cancel scope 0xdeadbeef")

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", cancelled)
    assert fetcher._parse_robinhood_options("SPY", 640.0, 25, 45) == []


def test_the_health_check_contains_a_cancellederror(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)

    def cancelled(symbol, url, option_type='put', **kw):
        raise asyncio.CancelledError("Cancelled via cancel scope 0xdeadbeef")

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", cancelled)
    assert fetcher.validate_robinhood_connection("SPY")["healthy"] is False


def test_a_cancellederror_still_latches_the_source_off(monkeypatch):
    """The latch has to fire on this path too, or a 56-ticker scan pays it 56 times."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    fetcher._robinhood_unavailable_this_run.clear()
    calls = []

    def cancelled(ticker, url, option_type='put', **kw):
        calls.append(ticker)
        raise asyncio.CancelledError("cancelled")

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", cancelled)
    for tk in ["SPY", "QQQ", "IWM"]:
        fetcher._parse_robinhood_options(tk, 100.0, 25, 45)
    assert calls == ["SPY"]


def test_operator_interrupts_are_not_swallowed(monkeypatch):
    """Catching BaseException must not eat Ctrl-C or a shutdown."""
    def interrupted(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(robinhood_mcp.asyncio, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        robinhood_mcp.fetch_put_chain("SPY", "https://example.invalid")


# ── What the first live cycle exposed ────────────────────────────────────────────────────
#
# Enabling this source on 2026-08-27 produced 104 yfinance readings and 1 Robinhood one. SPY
# was served correctly at 12:35:20 and then, one second later, a SECOND SPY call failed and
# disabled the source for all 56 tickers. Three separate defects stacked:
#
#   1. auto_paper_cycle marks positions via get_options_chain(ticker, 0, 200). That became 201
#      comma-separated dates -- a 2,210-character expiration_dates parameter -- and the server
#      stopped returning JSON. (21 dates / 230 chars is fine.)
#   2. The server answers errors as plain TEXT. The code called .get() on it, raising
#      "'str' object has no attribute 'get'" from inside an anyio task group, which surfaced as
#      an opaque ExceptionGroup with the real cause buried.
#   3. One empty answer latched the source off for the entire run -- far too eager for a
#      condition as ordinary as a thin name or an unusual DTE window.

def test_a_wide_dte_range_does_not_build_a_giant_parameter(monkeypatch):
    """The mark path asks for DTE 0-200. That must not become 201 dates in one parameter."""
    captured = {}

    async def fake_call(session, name, args):
        captured[name] = args
        return {"data": {"instruments": [], "next": None}}

    monkeypatch.setattr(robinhood_mcp, "_call_read_tool", fake_call)

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session(url):
        yield _Sess()

    monkeypatch.setattr(robinhood_mcp, "_robinhood_session", fake_session)
    _run(robinhood_mcp.afetch_chain("https://x", "SPY", spot=100.0,
                                        min_dte=0, max_dte=200))
    args = captured.get("get_option_instruments", {})
    dates = args.get("expiration_dates")
    assert dates is None or len(dates) < 1000, (
        "a 201-date parameter is what broke the live cycle; omit the filter instead")


def test_a_normal_window_still_uses_the_date_filter(monkeypatch):
    """The guard must not throw away the cheap server-side filter for ordinary scans."""
    captured = {}

    async def fake_call(session, name, args):
        captured[name] = args
        return {"data": {"instruments": [], "next": None}}

    monkeypatch.setattr(robinhood_mcp, "_call_read_tool", fake_call)

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session(url):
        yield _Sess()

    monkeypatch.setattr(robinhood_mcp, "_robinhood_session", fake_session)
    _run(robinhood_mcp.afetch_chain("https://x", "SPY", spot=100.0,
                                        min_dte=25, max_dte=45))
    assert captured["get_option_instruments"].get("expiration_dates"), \
        "a 21-date window is well inside the cap and should still be filtered server-side"


def test_a_text_error_response_does_not_raise(monkeypatch):
    """The server returns errors as text. Calling .get() on a str is what produced the
    ExceptionGroup that hid this for a whole cycle."""
    async def fake_call(session, name, args):
        return "Request entity too large"

    monkeypatch.setattr(robinhood_mcp, "_call_read_tool", fake_call)
    monkeypatch.setattr(robinhood_mcp, "_extract_tool_json", lambda r: r)

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session(url):
        yield _Sess()

    monkeypatch.setattr(robinhood_mcp, "_robinhood_session", fake_session)
    out = _run(robinhood_mcp.afetch_chain("https://x", "SPY", spot=100.0,
                                              min_dte=25, max_dte=45))
    assert out == {"instruments": [], "quotes": []}


def test_one_empty_answer_does_not_disable_the_whole_run(monkeypatch):
    """THE regression. One failure took 56 tickers off the source while it was working."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    fetcher._robinhood_unavailable_this_run.clear()
    fetcher._robinhood_failures.clear()
    calls = []

    def sometimes(ticker, url, option_type='put', **kw):
        calls.append(ticker)
        if ticker == "SPY":
            return None
        return {"instruments": [{"id": "x", "strike_price": "100.0",
                                 "expiration_date": "2099-01-01"}],
                "quotes": [{"instrument_id": "x", "bid_price": "1.0",
                            "ask_price": "1.1"}]}

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", sometimes)
    fetcher._parse_robinhood_options("SPY", 100.0, 25, 45)      # the one that fails
    fetcher._parse_robinhood_options("QQQ", 100.0, 25, 45)
    fetcher._parse_robinhood_options("IWM", 100.0, 25, 45)
    assert calls == ["SPY", "QQQ", "IWM"], f"source was written off after one miss: {calls}"


def test_repeated_failures_do_still_latch(monkeypatch):
    """The threshold must not be so forgiving that a dead source is retried 56 times."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    fetcher._robinhood_unavailable_this_run.clear()
    fetcher._robinhood_failures.clear()
    calls = []

    def always_empty(ticker, url, option_type='put', **kw):
        calls.append(ticker)
        return None

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", always_empty)
    for i in range(20):
        fetcher._parse_robinhood_options(f"T{i}", 100.0, 25, 45)
    assert len(calls) == fetcher._ROBINHOOD_FAILURE_LIMIT, \
        f"latched after {len(calls)}, expected {fetcher._ROBINHOOD_FAILURE_LIMIT}"


# ── P1: calls get the same pipeline as puts ──────────────────────────────────────────────
#
# Robinhood was wired in for ONE function -- put chains -- rather than as a data layer the
# system draws from. Bear-call spreads, iron-condor call legs, the lottery scanner and
# get_options_skew all still went straight to yfinance at 34-48% quotable while bull puts ran
# on Robinhood at ~91%. Skew scoring is disabled system-wide, and it is measured across BOTH
# wings, so the unserved call side was holding it down.

def _in_window_expiry(days=30):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def test_the_call_path_tries_robinhood_first(monkeypatch):
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    fetcher._cache.clear(); fetcher._quality_recorded.clear()
    fetcher._robinhood_unavailable_this_run.clear(); fetcher._robinhood_failures.clear()
    seen = {}

    def fake(ticker, url, option_type="put", **kw):
        seen[option_type] = ticker
        return {"instruments": [{"id": "x", "strike_price": "110.0",
                                 "expiration_date": _in_window_expiry()}],
                "quotes": [{"instrument_id": "x", "bid_price": "1.0", "ask_price": "1.1",
                            "delta": "0.2", "open_interest": 50, "volume": 10}]}

    monkeypatch.setattr(robinhood_mcp, "fetch_chain", fake)
    monkeypatch.setattr(fetcher, "_parse_yfinance_calls", lambda *a: [])   # never the network
    monkeypatch.setattr(fetcher, "get_price_data",
                        lambda t, **k: __import__("pandas").DataFrame({"Close": [100.0]}))
    out = fetcher.get_call_options_chain("SPY", 25, 45)
    assert seen.get("call") == "SPY", "the call path never asked Robinhood"
    assert out and out[0]["type"] == "call"


def test_the_call_path_falls_back_to_yfinance(monkeypatch):
    """Same graceful degradation the put side has. A dead source costs its own records."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    fetcher._cache.clear(); fetcher._quality_recorded.clear()
    fetcher._robinhood_unavailable_this_run.clear(); fetcher._robinhood_failures.clear()
    monkeypatch.setattr(robinhood_mcp, "fetch_chain",
                        lambda t, u, ot="put", **kw: None)
    called = {}
    def fake_yf(t, p, lo, hi):
        called["yf"] = True
        return []

    monkeypatch.setattr(fetcher, "_parse_yfinance_calls", fake_yf)
    monkeypatch.setattr(fetcher, "get_price_data",
                        lambda t, **k: __import__("pandas").DataFrame({"Close": [100.0]}))
    fetcher.get_call_options_chain("SPY", 25, 45)
    assert called.get("yf"), "call path did not fall back when Robinhood returned nothing"


def test_call_side_chain_quality_is_recorded(monkeypatch):
    """Call-side quality was never measured, so the cockpit tile described only half the
    data the engine trades on."""
    monkeypatch.setattr(config, "ROBINHOOD_MCP_ENABLED", True, raising=False)
    fetcher._cache.clear(); fetcher._quality_recorded.clear()
    fetcher._robinhood_unavailable_this_run.clear(); fetcher._robinhood_failures.clear()
    recorded = []
    monkeypatch.setattr(fetcher, "_record_chain_quality",
                        lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(robinhood_mcp, "fetch_chain",
                        lambda t, u, ot="put", **kw: {
                            "instruments": [{"id": "x", "strike_price": "110.0",
                                             "expiration_date": _in_window_expiry()}],
                            "quotes": [{"instrument_id": "x", "bid_price": "1.0",
                                        "ask_price": "1.1", "open_interest": 5,
                                        "volume": 5}]})
    monkeypatch.setattr(fetcher, "get_price_data",
                        lambda t, **k: __import__("pandas").DataFrame({"Close": [100.0]}))
    fetcher.get_call_options_chain("SPY", 25, 45)
    assert recorded, "call-side chain quality went unrecorded"
    assert recorded[0][1] == "robinhood"


def test_the_put_and_call_strike_windows_are_on_opposite_sides(monkeypatch):
    """A put seller works below spot, a call seller above it. Reusing the put window for calls
    would fetch deep-ITM calls and miss every strike a bear call could use."""
    captured = {}

    async def fake_call(session, name, args):
        captured.setdefault(name, []).append(args)
        return {"data": {"instruments": [], "next": None}}

    import contextlib

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    @contextlib.asynccontextmanager
    async def fake_session(url):
        yield _S()

    monkeypatch.setattr(robinhood_mcp, "_call_read_tool", fake_call)
    monkeypatch.setattr(robinhood_mcp, "_robinhood_session", fake_session)
    for side in ("put", "call"):
        _run(robinhood_mcp.afetch_chain("https://x", "SPY", side, spot=100.0,
                                        min_dte=25, max_dte=45))
    types = [a.get("type") for a in captured["get_option_instruments"]]
    assert types == ["put", "call"], f"option_type not threaded through: {types}"


# ── Fetch cost ───────────────────────────────────────────────────────────────────────────
#
# Enabling Robinhood for calls as well as puts doubled the per-ticker MCP work and pushed cycle
# runtime from ~17.5 min to 29.7 min against a 30-minute Task Scheduler kill -- 18 seconds of
# headroom, and the same daily close-scan death arriving from a new direction. The limit was
# raised to 45 min as immediate cover; this is the actual fix.

def test_the_quote_batch_size_stays_at_twenty():
    """get_option_quotes SILENTLY TRUNCATES above 20 instrument ids.

    Measured live on 2026-08-28 against the real server:
        20 ids  -> 20 rows
        50 ids  -> 43 rows   (7 dropped, no error)
        100 ids -> 81 rows  (19 dropped, no error)

    Raising the batch is the obvious way to cut the number of quote calls, and it would silently
    lose 14-19% of every chain. The loss looks exactly like a thin underlying, not like a bug.
    Fetch fewer STRIKES instead -- see _STRIKE_LO_PCT/_STRIKE_HI_PCT.
    """
    assert robinhood_mcp._QUOTE_BATCH <= 20, (
        "above 20 ids the server drops rows without erroring; cut strikes, not calls")


def test_the_strike_window_still_covers_the_usable_delta_band():
    """Narrowed from 60-105% to 75-102% of spot to cut fetch cost.

    Every strike inside VEGA's usable delta range (.10-.35) measured between 82% and 99% of
    spot across SPY, NKE, XLE and AMD; the widest was AMD at 82%. Selection was verified to
    pick the identical pair on all four before and after. Keep a real margin below the widest
    observed case -- a high-IV name puts its 20-delta strike further out.
    """
    assert robinhood_mcp._STRIKE_LO_PCT <= 0.78, (
        "AMD's usable strikes reached 82% of spot; leave margin below that")
    assert robinhood_mcp._STRIKE_HI_PCT >= 1.00, (
        "the short strike can sit at the money; do not cut below spot")
    assert robinhood_mcp._STRIKE_HI_PCT <= 1.10, (
        "strikes well above spot are ITM puts this strategy never sells")
