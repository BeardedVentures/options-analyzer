"""
data/robinhood_mcp.py -- Robinhood Agentic Trading MCP client.

Talks to Robinhood's OFFICIAL Trading MCP server (agent.robinhood.com/mcp/trading),
launched 2026, NOT the unofficial robin_stocks-style private-API wrappers. This is a
sanctioned, ToS-safe way to pull options chains/quotes/Greeks from the same Robinhood
account VEGA already trades and banks through -- no second brokerage account needed.

Auth: standard remote-MCP OAuth (browser approval, once). The resulting token is cached
to `data/.robinhood_mcp_tokens.json` (gitignored) and reused on every subsequent run, so
this should only prompt a browser approval again if that token is later revoked/expires
server-side -- see ROBINHOOD_MCP_NOTES.md for what to do if that happens more often than
expected.

IMPORTANT -- first-run status: the exact MCP tool names/parameter shapes below
("get_option_chains", "get_option_quotes") are Robinhood's own stated names from their
public support docs, not yet confirmed against a live call. Run
`test_robinhood_mcp_connection.py` once, standalone, before trusting this in a real scan --
it prints the server's real tool list/schemas and a sample response so the field-mapping
in fetcher.py's `_parse_robinhood_options()` can be corrected against real data rather than
guesses. Treat this module as "should work" not "confirmed working" until that test passes.

Requires: pip install mcp   (added to requirements.txt)
"""

import asyncio
from datetime import date, timedelta
import contextlib
import http.server
import sys
import json
import logging
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TOKEN_PATH = Path(__file__).parent / ".robinhood_mcp_tokens.json"
_CALLBACK_PORT = 3030
_CALLBACK_PATH = "/callback"

# Cached across calls within a process so repeated fetches in one scan don't re-import
# or re-probe tool schemas every time.
_tools_cache: Optional[List[str]] = None


def _require_mcp_sdk():
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "The 'mcp' package is not installed. Run: pip install mcp"
        ) from exc


# ─────────────────────────────────────────────
# Token storage -- persists across process runs so OAuth only happens once
# ─────────────────────────────────────────────

class _FileTokenStorage:
    """Implements the mcp SDK's TokenStorage protocol, backed by a local JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self._tokens = None
        self._client_info = None
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
            raw = json.loads(self.path.read_text())
            if raw.get("tokens"):
                self._tokens = OAuthToken.model_validate(raw["tokens"])
            if raw.get("client_info"):
                self._client_info = OAuthClientInformationFull.model_validate(raw["client_info"])
        except Exception as exc:
            logger.warning(f"[robinhood_mcp] Could not load cached tokens ({exc}); will re-authorize.")

    def _save(self):
        data = {}
        if self._tokens is not None:
            data["tokens"] = self._tokens.model_dump(mode="json", exclude_none=True)
        if self._client_info is not None:
            data["client_info"] = self._client_info.model_dump(mode="json", exclude_none=True)
        try:
            self.path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning(f"[robinhood_mcp] Could not persist tokens: {exc}")

    async def get_tokens(self):
        return self._tokens

    async def set_tokens(self, tokens):
        self._tokens = tokens
        self._save()

    async def get_client_info(self):
        return self._client_info

    async def set_client_info(self, client_info):
        self._client_info = client_info
        self._save()


# ─────────────────────────────────────────────
# Local loopback callback server -- catches the OAuth redirect after browser approval
# ─────────────────────────────────────────────

class _OAuthCallbackServer:
    def __init__(self, port: int = _CALLBACK_PORT):
        self.port = port
        self._code = None
        self._state = None
        self._error = None
        self._event = threading.Event()
        self._server = None
        self._thread = None

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != _CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                outer._code = qs.get("code", [None])[0]
                outer._state = qs.get("state", [None])[0]
                outer._error = qs.get("error", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                if outer._code:
                    msg = "Robinhood connected -- you can close this tab and return to VEGA."
                else:
                    msg = f"Authorization failed: {outer._error or 'unknown error'}"
                self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())
                outer._event.set()

            def log_message(self, *args):
                pass  # silence default request logging

        self._server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    async def wait_for_callback(self, timeout: float = None):
        if timeout is None:
            try:
                import config
                timeout = float(getattr(config, "ROBINHOOD_MCP_CALLBACK_TIMEOUT", 120))
            except Exception:
                timeout = 120.0
        loop = asyncio.get_event_loop()
        got_it = await loop.run_in_executor(None, self._event.wait, timeout)
        if not got_it:
            raise TimeoutError(
                "Timed out waiting for Robinhood authorization in the browser "
                f"(waited {timeout:.0f}s). Re-run and approve the browser prompt promptly."
            )
        if self._error:
            raise RuntimeError(f"Robinhood OAuth error: {self._error}")
        if not self._code:
            raise RuntimeError("Robinhood OAuth callback returned no authorization code.")
        # mcp 2.x reads result.code / result.state off an AuthorizationCodeResult. The 1.x
        # contract was a bare (code, state) tuple, which reached the SDK as:
        #   AttributeError: 'tuple' object has no attribute 'state'
        # after the browser approval had already succeeded -- the very last step of the
        # flow, and the third place the 1.x/2.x API break surfaced.
        from mcp.shared.auth import AuthorizationCodeResult
        return AuthorizationCodeResult(code=self._code, state=self._state)

    def stop(self):
        if self._server:
            self._server.shutdown()


def _interactive_auth_allowed() -> bool:
    """Is a human present to approve a browser prompt?

    Two independent conditions, because either alone is too weak. The env flag is the
    explicit opt-in (only the standalone connection test sets it). The TTY check catches
    the case where the flag is left set in a profile that the scheduled task inherits.
    """
    try:
        import config
        if not getattr(config, "ROBINHOOD_MCP_ALLOW_BROWSER", False):
            return False
    except Exception:
        return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


class InteractiveAuthRequired(RuntimeError):
    """Raised instead of opening a browser in a non-interactive process."""


async def _redirect_handler(auth_url: str) -> None:
    if not _interactive_auth_allowed():
        # Fail immediately rather than opening a tab nobody will see and then blocking
        # for the callback timeout. The hourly cycle runs hidden and non-interactive; a
        # blocking prompt there costs the timeout PER TICKER and would push a 11-16 min
        # cycle past the 30 min ExecutionTimeLimit -- re-creating, from a new direction,
        # exactly the daily close-scan death that was fixed on 2026-08-25.
        raise InteractiveAuthRequired(
            "Robinhood MCP needs a one-time browser approval and this process is not "
            "interactive. Run test_robinhood_mcp_connection.py from a terminal to "
            "authorize once; the cached refresh token then serves unattended runs."
        )
    print("\n" + "=" * 70)
    print("ROBINHOOD AUTHORIZATION REQUIRED")
    print("Open this URL and approve VEGA's read access to options data:")
    print(auth_url)
    print("=" * 70 + "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass  # printed URL is enough if auto-open fails (e.g. headless)


# ─────────────────────────────────────────────
# Session management
# ─────────────────────────────────────────────

@contextlib.asynccontextmanager
async def _robinhood_session(server_url: str):
    """One authenticated MCP session, opened and closed inside a single task.

    This was a class that stored `streamable_http_client(...)` and `ClientSession(...)` on
    self and drove their __aenter__/__aexit__ from its own __aenter__/__aexit__. That looks
    equivalent to nesting `async with` blocks and is not:

        RuntimeError: Attempted to exit cancel scope in a different task than it was entered in

    anyio binds a cancel scope to the task that entered it, and the MCP transport runs its
    reader and writer in a task group. Entering the scope in one task and leaving it in
    another is unsupported, so the session blew up during teardown -- after the OAuth URL had
    already been issued, which made it look like an auth problem rather than a structural one.

    Nesting the context managers keeps every enter and exit in this coroutine, where anyio
    expects them. The auth flow itself was already correct: dynamic client registration
    succeeded against Robinhood on 2026-08-27 and the resulting client_info is cached.
    """
    _require_mcp_sdk()
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    callback_server = _OAuthCallbackServer(_CALLBACK_PORT)
    callback_server.start()

    async def callback_handler():
        return await callback_server.wait_for_callback()

    oauth_provider = OAuthClientProvider(
        # The FULL MCP url, not the origin. Robinhood scopes its protected-resource metadata
        # per path: the 401 names /.well-known/oauth-protected-resource/mcp/trading and the
        # issuer is https://agent.robinhood.com/mcp/trading.
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="VEGA Options Scanner",
            redirect_uris=[f"http://127.0.0.1:{_CALLBACK_PORT}{_CALLBACK_PATH}"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=_FileTokenStorage(_TOKEN_PATH),
        redirect_handler=_redirect_handler,
        callback_handler=callback_handler,
    )

    try:
        async with httpx2.AsyncClient(auth=oauth_provider, timeout=30.0) as http_client:
            # terminate_on_close=False: on close the SDK otherwise sends
            #   DELETE /mcp/trading  ->  400 Bad Request  ("Session termination failed")
            # because Robinhood's server does not implement the optional MCP session-
            # termination verb. That 400 raised inside the transport's task group and
            # surfaced as "unhandled errors in a TaskGroup" -- AFTER a fully successful
            # authenticated session, so a working call looked like a failed one. The
            # session is short-lived and server-expired anyway; not sending the DELETE
            # costs nothing.
            async with streamable_http_client(server_url, http_client=http_client,
                                              terminate_on_close=False) as streams:
                # 1.x yielded (read, write, get_session_id); 2.x yields a TransportStreams
                # tuple. Take the first two positionally so a third element -- or its absence
                # -- does not matter here.
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
    finally:
        # Always release the callback listener, including when the handshake fails. Leaving it
        # bound means the next attempt cannot start its listener on the same port.
        callback_server.stop()


def _extract_tool_json(result) -> Any:
    """CallToolResult -> parsed JSON, trying structuredContent first, then text content."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


# ─────────────────────────────────────────────
# Public async API
# ─────────────────────────────────────────────

async def alist_tools(server_url: str) -> List[Dict[str, Any]]:
    """List every tool the server exposes, with its input schema. Diagnostic use."""
    async with _robinhood_session(server_url) as session:
        result = await session.list_tools()
        return [
            # mcp 2.x renamed inputSchema -> input_schema. getattr covers both so this
            # diagnostic keeps working across SDK versions.
            {"name": t.name, "description": t.description,
             "input_schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)}
            for t in result.tools
        ]


# Quotes are requested by instrument UUID, and the tool's own schema warns that past 20 ids
# the response degrades. Batch conservatively.
_QUOTE_BATCH = 20

# Strikes outside this band cost quote requests and are never selected. Measured across SPY,
# NKE, XLE and AMD on 2026-08-28, every strike inside VEGA's usable delta range (.10-.35) fell
# between 82% and 99% of spot; the widest was AMD at 82%. 75%-102% keeps a 7-point margin below
# the widest observed case and covers the long leg sitting beneath the short, while dropping the
# deep-OTM tail and the ITM strikes above spot that a put seller never touches.
#
# This is the only lever available on fetch cost: get_option_quotes SILENTLY TRUNCATES above 20
# instrument ids -- 50 ids returned 43 rows and 100 returned 81, with no error -- so the batch
# size cannot be raised to reduce the number of calls. Fetching fewer strikes is the alternative.
_STRIKE_LO_PCT = 0.75
_STRIKE_HI_PCT = 1.02


# get_option_instruments returns 100 per page. A full SPY put chain across a 20-day window is
# several hundred contracts, so cap the walk rather than trusting the server to terminate it.
_MAX_INSTRUMENT_PAGES = 12

# Above this many dates the expiration_dates parameter gets long enough that the server stops
# returning JSON. 201 dates (2,210 chars) reproducibly failed on 2026-08-27; 21 dates (230
# chars) is fine. Keep a wide margin -- this is a guard, not a measured ceiling.
_MAX_EXPIRATION_DATES = 60


def _next_cursor(next_url):
    """Pull the `cursor` query parameter out of the tool's `next` link.

    `next` is a fully-qualified internal Robinhood URL
    (http://edge-internal.brokeback-shard-router...), which is NOT reachable from here and must
    not be fetched directly -- only the cursor value it carries is usable, fed back into the
    tool's own `cursor` argument.
    """
    if not next_url:
        return None
    try:
        return urllib.parse.parse_qs(
            urllib.parse.urlparse(str(next_url)).query).get("cursor", [None])[0]
    except Exception:                                # pragma: no cover - defensive
        return None


async def afetch_chain(server_url: str, ticker: str, option_type: str = "put",
                       spot: float = None,
                       min_dte: int = None, max_dte: int = None,
                       expirations=None, strikes=None) -> Dict[str, Any]:
    """Fetch contracts of `option_type` and their quotes for `ticker`.

    THREE calls, not two, because that is what the server implements. The original code called
    get_option_chains(symbol=...) then get_option_quotes(symbol=..., option_type=...), which was
    a reasonable guess from the published tool names and wrong in every particular. Read off the
    live schemas on 2026-08-27:

      get_option_chains       takes `underlying_symbol` / `ids`, and returns CHAIN metadata --
                              tick sizes and multipliers, not contracts.
      get_option_instruments  takes `chain_symbol`, `type`, `expiration_dates`, `state` and
                              returns the contracts, each with a UUID `id`, `strike_price`
                              (string), `expiration_date`, and a pagination `next`.
      get_option_quotes       REQUIRES `instrument_ids` -- an array of those UUIDs. There is no
                              symbol form.

    So the contracts have to be listed first and their ids fed to the quote call. Returns the
    raw pieces; fetcher._parse_robinhood_options does the normalisation, same division of
    labour as the Polygon and yfinance parsers.
    """
    import config as _cfg
    min_dte = _cfg.MIN_DTE if min_dte is None else min_dte
    max_dte = _cfg.MAX_DTE if max_dte is None else max_dte

    today = date.today()
    # The tool filters server-side on exact dates, so for a normal scan window hand it every
    # date and let it return the ones that exist -- more robust than predicting which Fridays a
    # given underlying lists, since SPY has near-daily expiries and some names are monthly only.
    #
    # But ONLY for a normal window. auto_paper_cycle calls get_options_chain(ticker, 0, 200) to
    # mark open positions, which produced 201 dates in a 2,210-character parameter; the server
    # answered with a plain-text error instead of JSON, and the whole source latched off for the
    # rest of the run on the first ticker. Past the cap, omit the filter and let pagination plus
    # the strike window bound the walk, with the DTE filter applied client-side in the parser.
    span = [(today + timedelta(days=d)).isoformat()
            for d in range(max(min_dte, 0), max_dte + 1)]
    # CHUNK the window rather than dropping the filter past the cap. Omitting it looks
    # equivalent -- the parser filters DTE client-side anyway -- and is not:
    # get_option_instruments paginates by ASCENDING STRIKE across every expiration it can see,
    # so with no date filter the first pages are the lowest strikes of the entire chain.
    # Against the narrowed strike window (75-102% of spot) none of them qualify, the early-stop
    # never fires because those strikes are BELOW the window rather than above it, and the walk
    # exhausts its page budget having kept nothing. SPY DTE 5-120 returned 0 instruments in the
    # 09:08 scan on 2026-08-28 for exactly this reason, which then sent the run to Polygon and
    # yfinance. Chunking keeps the server-side filter -- which is what makes pagination
    # tractable -- for any width of DTE range.
    # An explicit expiration list beats the computed span. Marking an open position knows the
    # exact expiry, so asking for a 200-day window to price a single known contract fetched
    # 1,541 instruments and ~40 s per ticker to use two of them.
    if expirations:
        span = sorted({str(e)[:10] for e in expirations if e})
    date_chunks = [span[i:i + _MAX_EXPIRATION_DATES]
                   for i in range(0, len(span), _MAX_EXPIRATION_DATES)] or [None]

    # The useful strike band is on opposite sides for the two option types. A put seller works
    # below spot; a call seller works above it. Using the put window for calls would fetch
    # deep-ITM calls and miss every strike a bear-call or condor could actually use.
    # Explicit strikes bypass the percentage band ENTIRELY. The band is a selection
    # optimisation -- it exists so a scan does not quote strikes it would never sell -- and it
    # must never decide whether an already-open position can be priced. A put spread that is
    # winning has seen the underlying rally AWAY from it, so its strikes drift toward the
    # bottom of the band and eventually out of it: the better the position, the more likely the
    # window would stop being able to see it. Marking asks a different question from selection
    # and gets a different filter.
    want_strikes = None
    if strikes:
        try:
            want_strikes = {round(float(k), 2) for k in strikes}
        except (TypeError, ValueError):
            want_strikes = None
    if want_strikes:
        lo = hi = None
    elif spot:
        if option_type == "call":
            lo, hi = spot * (2 - _STRIKE_HI_PCT), spot * (2 - _STRIKE_LO_PCT)
        else:
            lo, hi = spot * _STRIKE_LO_PCT, spot * _STRIKE_HI_PCT
    else:
        lo = hi = None

    async with _robinhood_session(server_url) as session:
        # PAGINATED, 100 per page, ordered by STRIKE ASCENDING. This matters more than it
        # sounds: page one for SPY on 2026-08-27 was strikes 375-605 against a spot of 771 --
        # every contract on it hundreds of points out of the money, delta ~0.00. Reading only
        # the first page produced 82 perfectly-quoted records and ZERO inside VEGA's 0.12-0.30
        # delta band, which looks exactly like "this underlying has nothing to sell" rather
        # than like a truncated fetch.
        instruments = []
        for chunk in date_chunks:
            cursor = None
            for _page in range(_MAX_INSTRUMENT_PAGES):
                args = {
                    "chain_symbol": ticker.upper(),
                    "type": option_type,
                    "state": "active",
                    "tradability": "tradable",
                }
                if chunk:
                    args["expiration_dates"] = ",".join(chunk)
                if cursor:
                    args["cursor"] = cursor
                inst_result = await _call_read_tool(session, "get_option_instruments", args)
                raw = _extract_tool_json(inst_result)
                if not isinstance(raw, dict):
                    # The server answers errors as text, not JSON. Treating that as a dict raised
                    # "AttributeError: 'str' object has no attribute 'get'" from inside the anyio
                    # task group, which surfaced as an opaque ExceptionGroup.
                    logger.warning("[robinhood_mcp] %s: non-JSON response from "
                                   "get_option_instruments: %s", ticker, str(raw)[:200])
                    break
                payload = (raw.get("data") or {})
                page = payload.get("instruments") or []
                if not page:
                    break

                page_strikes = []
                for i in page:
                    try:
                        page_strikes.append(float(i.get("strike_price")))
                    except (TypeError, ValueError):
                        continue
                    if want_strikes is not None:
                        if round(page_strikes[-1], 2) in want_strikes:
                            instruments.append(i)
                    elif lo is None or lo <= page_strikes[-1] <= hi:
                        instruments.append(i)

                # Ascending order lets us stop as soon as a page opens above the useful range,
                # instead of walking every LEAPS strike to the top of the chain.
                if hi is not None and page_strikes and min(page_strikes) > hi:
                    break
                cursor = _next_cursor(payload.get("next"))
                if not cursor:
                    break

        if not instruments:
            logger.warning("[robinhood_mcp] no %s instruments for %s in DTE %s-%s",
                           option_type, ticker, min_dte, max_dte)
            return {"instruments": [], "quotes": []}

        by_id = {i["id"]: i for i in instruments if i.get("id")}
        ids = list(by_id.keys())

        quotes = []
        for n in range(0, len(ids), _QUOTE_BATCH):
            batch = ids[n:n + _QUOTE_BATCH]
            q_result = await _call_read_tool(session, "get_option_quotes",
                                             {"instrument_ids": batch})
            q_payload = _extract_tool_json(q_result)
            if not isinstance(q_payload, dict):
                logger.warning("[robinhood_mcp] %s: non-JSON response from get_option_quotes: "
                               "%s", ticker, str(q_payload)[:200])
                continue
            for row in ((q_payload.get("data") or {}) or {}).get("results") or []:
                quote = row.get("quote") if isinstance(row, dict) else None
                if quote:
                    quotes.append(quote)

        logger.debug("[robinhood_mcp] %s: %d instruments, %d quotes",
                     ticker, len(instruments), len(quotes))
        return {"instruments": list(by_id.values()), "quotes": quotes}


# ─────────────────────────────────────────────
# READ-ONLY ENFORCEMENT
# ─────────────────────────────────────────────
#
# This connects to Robinhood's Agentic TRADING MCP server, against a real brokerage account,
# and the OAuth grant is a single broad scope ("internal") -- there is no read-only scope to
# ask for. The server therefore also exposes order-placement tools, and the token minted here
# would very likely be accepted for them.
#
# VEGA's contract is explicit and non-negotiable: this integration is a DATA SOURCE. Every
# order is initiated and executed by a human, in Robinhood's own interface. Nothing here may
# place, modify, or cancel an order.
#
# "Our code only happens to call read tools" is not that guarantee -- it is a property of the
# current call sites, which a later edit, a copied snippet, or a mistaken tool name could
# quietly change. An allowlist is the guarantee: a tool that is not named here cannot be
# invoked through this module at all, and adding one is a deliberate, reviewable act.
READ_ONLY_TOOLS = frozenset({
    "get_option_chains",
    "get_option_quotes",
    "get_option_historicals",
    "get_option_instruments",
    # Added deliberately 2026-08-28. The server grew from 47 to 55 tools overnight and this one
    # appeared with it -- blocked by default until reviewed, which is the point of the list.
    # Read-only by contract: "Get recent news articles for a stock, resolved by ticker symbol."
    # Replaces a NewsAPI free tier that answers 429 to nearly every call.
    "get_equity_news",
})


class WriteToolBlocked(RuntimeError):
    """Raised when anything tries to invoke a non-read-only tool through this module."""


async def _call_read_tool(session, name: str, args: dict):
    """The ONLY path from this module to the MCP server. Refuses anything not allowlisted."""
    if name not in READ_ONLY_TOOLS:
        raise WriteToolBlocked(
            f"Refusing to call Robinhood MCP tool {name!r}: not in READ_ONLY_TOOLS. "
            f"VEGA is a data source; orders are placed by a human in Robinhood. If this tool "
            f"is genuinely read-only, add it to READ_ONLY_TOOLS deliberately."
        )
    return await session.call_tool(name, args)


# ─────────────────────────────────────────────
# Sync wrapper -- fetcher.py is a synchronous module (uses `requests`, not asyncio)
# ─────────────────────────────────────────────

def fetch_chain(ticker: str, server_url: str, option_type: str = "put",
                spot: float = None, min_dte: int = None, max_dte: int = None,
                expirations=None, strikes=None) -> Optional[Dict[str, Any]]:
    """Sync entry point for fetcher.py. Returns None on any failure -- never raises,
    matching the graceful-degradation contract of every other parser in fetcher.py.

    Catches BaseException, not Exception, and that is the whole point of this docstring.
    The MCP client runs its transport inside an anyio task group, and a failure raised in
    there -- including the InteractiveAuthRequired this module raises on purpose -- comes back
    out as `asyncio.CancelledError`. CancelledError derives from BaseException, NOT Exception,
    so the obvious `except Exception` silently did not apply: verified 2026-08-27, an auth
    refusal escaped this function and then escaped fetcher._parse_robinhood_options too,
    straight into the scan. A data source that cannot reach its server must cost its own
    records and nothing else.

    KeyboardInterrupt and SystemExit are re-raised: those are the operator, not the source.
    """
    try:
        return asyncio.run(afetch_chain(server_url, ticker, option_type, spot=spot,
                                        min_dte=min_dte, max_dte=max_dte,
                                        expirations=expirations, strikes=strikes))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        logger.warning(f"[robinhood_mcp] {option_type} fetch failed for {ticker} "
                       f"({type(exc).__name__}: {exc})", exc_info=True)
        return None



def fetch_put_chain(ticker: str, server_url: str, spot: float = None,
                    min_dte: int = None, max_dte: int = None) -> Optional[Dict[str, Any]]:
    """Puts. Thin wrapper over fetch_chain, kept because callers and tests already use it."""
    return fetch_chain(ticker, server_url, "put", spot=spot,
                       min_dte=min_dte, max_dte=max_dte)


def fetch_call_chain(ticker: str, server_url: str, spot: float = None,
                     min_dte: int = None, max_dte: int = None) -> Optional[Dict[str, Any]]:
    """Calls. The bear-call, iron-condor and lottery paths were still on yfinance at 34-48%
    quotable while the put side ran on Robinhood at ~91%; this is the other half."""
    return fetch_chain(ticker, server_url, "call", spot=spot,
                       min_dte=min_dte, max_dte=max_dte)

async def afetch_news(server_url: str, ticker: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Recent articles for one symbol, via the official MCP server.

    Discovered 2026-08-28: the server exposes get_equity_news, and it returns rather more than
    NewsAPI's free tier ever did -- title, publisher, published_at, a preview AND the full
    article body. It arrived in an overnight expansion from 47 tools to 55, blocked by
    READ_ONLY_TOOLS until reviewed, which is what that list is for.

    This matters because NewsAPI's free tier answers 429 to nearly every call (3,688 of them in
    one log), so per-ticker sentiment had been silently running on keyword fallback.
    """
    async with _robinhood_session(server_url) as session:
        result = await _call_read_tool(session, "get_equity_news",
                                       {"symbol": ticker.upper(), "limit": int(limit)})
        payload = _extract_tool_json(result)
        if not isinstance(payload, dict):
            logger.warning("[robinhood_mcp] %s: non-JSON news response: %s",
                           ticker, str(payload)[:160])
            return []
        return ((payload.get("data") or {}).get("articles") or [])


def fetch_news(ticker: str, server_url: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Sync wrapper. Returns [] on any failure -- news is advisory and must never fail a scan."""
    try:
        return asyncio.run(afetch_news(server_url, ticker, limit))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        logger.warning("[robinhood_mcp] news fetch failed for %s (%s: %s)",
                       ticker, type(exc).__name__, exc)
        return []


def list_tools(server_url: str) -> List[Dict[str, Any]]:
    """Sync wrapper around alist_tools -- used by the standalone connection test.

    Unwraps the anyio cancellation so the diagnostic prints the real cause rather than
    "Cancelled via cancel scope 0x...". Deliberately still RAISES: this one is the
    interactive test, where a failure should be loud, not swallowed.
    """
    try:
        return asyncio.run(alist_tools(server_url))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        cause = exc
        seen = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, InteractiveAuthRequired):
                raise cause
            cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
        raise
